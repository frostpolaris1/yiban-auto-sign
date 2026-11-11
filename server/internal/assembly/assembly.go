// Package assembly 是 Go 服务端的组合根（composition root）。
//
// # 功能
// 装配配置、数据库、会话签发器、全部业务域与路由，并把结果作为
// Stack 交给调用方；Run 服务到 SIGINT 或 SIGTERM。
// cmd/server 与 cmd/cli 的 server 子命令都经由本包启动进程，
// 两条入口共享同一份装配代码，不复制。
// 本包**不**解析命令行参数、**不**打印用法、**不**退出进程。
//
// # 归属取舍
// 候选一：internal/app/bootstrap——被否：canonical.tsv 的 boundary.app
// 禁止 app 层导入 adapters 与 entry，而组合根必须同时导入两者；
// 为它开洞等于削弱既有门。候选二：直接导出 cmd/server 的 Run——
// 不可达：package main 的标识符无法被其他包导入。故新设 internal/assembly：
// structure.tsv 登记为 internal 的子域；canonical.tsv 把 assembly 加进
// 四条 boundary 规则并新增「internal 下不得导入 assembly」，
// 保证它只被 cmd 引用，不成为新的横向依赖通道。
//
// # 复用
// Config、Stack、Build、NewServer、Run；
// WorkerNotifier、ScheduleNotifier（事件总线适配，测试复用同一形状）。
//
// # 口径
// 优雅关闭上限 10s。
// 无数据库时认证端点不可用，进程打印警告，健康检查仍可服务。
// 有数据库时启动即起执行循环（见 run）：认领到期任务并结算。
package assembly

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	mailadapter "github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/session"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/storage"
	webhookadapter "github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/webhook"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/audit"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appcalendar "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/calendar"
	appdiagnostic "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/diagnostic"
	appegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/egress"
	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	applocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/location"
	appnotify "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/notify"
	appphotos "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/photos"
	appretention "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/retention"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	appadmin "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schooladmin"
	appsettings "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/settings"
	appstats "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/stats"
	apptimepref "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/timepref"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corecapacity "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/capacity"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreimage "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/image"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	coreplan "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/plan"
	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	corewebhook "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/webhook"
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	adminhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/admin"
	audithttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/audit"
	calendarhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/calendar"
	eventshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/events"
	locationhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/location"
	notifyhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/notify"
	photoshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/photos"
	ratelimit "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/ratelimit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	schedulehttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/schedule"
	schoolhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/school"
	signinghttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/signing"
	systemhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/system"
	userhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/providers/yiban"
)

// 配额上限不再有编译期常量（批 r4r）：两枚 DB-only 键
// quota.max_users / quota.max_accounts 经覆盖链解析，缺行=1（master 自用紧档），
// 0=不限。装配与 CLI 共用 appsettings.Resolver 这一条读链。

// 图片预处理上限：4000 万像素、单边 8192。
// 覆盖主流手机原图；超限在解码前拒绝，避免解压炸弹。
const (
	photoMaxPixels = 40_000_000
	photoMaxSidePx = 8192
)

// shutdownBudget 是优雅关闭的总预算：停执行循环与停 HTTP 并行，
// 各自在预算内收尾，超时不再等。
const shutdownBudget = 10 * time.Second

// Run 装配并服务到 ctx 结束。错误只来自装配失败与关闭失败。
//
// # 一次装配
// Run 只装配一次，并把同一份 Stack 交给 HTTP 与执行循环。
// 双装配会带来双连接池、双迁移与双 Recover；更严重的是
// 执行循环发布事件进第一份 broker，SSE 订阅第二份，
// 管理员永远收不到任务事件。故禁止调用 NewServer。
//
// 有数据库时同时启动执行循环：周期认领到期任务并结算。
// ctx 结束后并行做两件事：停执行循环的领取并等在飞结算、
// 停 HTTP 的在途请求。两者共享 shutdownBudget。
func Run(ctx context.Context, logger *slog.Logger) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	stack, err := buildStack(cfg, logger)
	if err != nil {
		return err
	}
	srv := newServerFromStack(cfg, stack, false)

	// 执行循环只在有数据库（Worker 已装配）时启动；
	// 批量取 Workers：认领 SQL 每账号至多一行，批内并行度与之对齐。
	var loop loopHandle
	if stack.Worker != nil {
		loop = newLoop(stack, workerPollInterval, cfg.Workers)
		if err := loop.Start(); err != nil {
			// 循环没起来也要释放连接池：别把半个进程留给退出路径。
			stack.Close()
			return fmt.Errorf("server: start worker loop: %w", err)
		}
		logger.Info("worker: loop started", logfield.KeyPollInterval, workerPollInterval.String(), logfield.KeyBatch, cfg.Workers)
	}

	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()

	select {
	case <-ctx.Done():
		return stopStack(srv, stack, loop, logger)
	case err := <-errc:
		if errors.Is(err, http.ErrServerClosed) {
			return stopStack(srv, stack, loop, logger)
		}
		if loop != nil {
			bounded, cancel := context.WithTimeout(context.Background(), shutdownBudget)
			_, _ = loop.Stop(bounded)
			cancel()
		}
		stack.Close()
		return err
	}
}

// workerPollInterval 是执行循环的空转轮询间隔。
// 认领 SQL 只挑到期行，轮询成本是一次索引查询。
const workerPollInterval = 2 * time.Second

// stopStack 并行收尾执行循环与 HTTP，总预算 shutdownBudget。
//
// 顺序判据：先停 worker 领取，再等在飞结算，
// 最后打印在飞清零结果；HTTP 关闭与它并行，不互相等。
// 预算耗尽而循环未停时，残余 running 行的租约到期后由回收器回收；下次启动的 Recover 清无租约或已过期行。
func stopStack(srv *http.Server, stack *Stack, loop loopHandle, logger *slog.Logger) error {
	shutdown, cancel := context.WithTimeout(context.Background(), shutdownBudget)
	defer cancel()

	var wg sync.WaitGroup
	var remain int64
	var loopErr error
	if loop != nil {
		wg.Add(1)
		go func() {
			defer wg.Done()
			remain, loopErr = loop.Stop(shutdown)
		}()
	}
	srvErr := srv.Shutdown(shutdown)
	wg.Wait()
	stack.Close()

	if loop != nil {
		switch {
		case errors.Is(loopErr, context.DeadlineExceeded):
			logger.Warn("worker: in-flight drain budget exhausted; remaining rows recover on next boot")
		case loopErr != nil:
			logger.Error("worker: drain bookkeeping failed", logfield.KeyError, loopErr.Error())
		default:
			logger.Info("worker: in-flight drained", logfield.KeyRemaining, remain)
		}
		if err := loop.Err(); err != nil {
			logger.Warn("worker: loop recorded a step error before shutdown", logfield.KeyError, err.Error())
		}
	}
	if srvErr != nil {
		return fmt.Errorf("assembly: http shutdown: %w", srvErr)
	}
	return nil
}

// Stack 是装配结果。Engine 已就绪；DB 在无数据库时为 nil。
type Stack struct {
	Engine http.Handler
	DB     *pg.DB
	// Worker 是执行循环用例。无数据库时为 nil。
	Worker *appworker.Service
	// EngineState 是执行循环的进程内时刻记录，与 HTTP 的执行监控端点
	// 共享同一实例。无数据库时为 nil。
	EngineState *appworker.EngineState
	Close       func()
}

// buildStack 是 Build 的调用接缝。
// Run 与 NewServer 都经由它；装配次数由测试计数（见 assembly_test.go）。
var buildStack = Build

// loopHandle 是执行循环的启停面。Run 只依赖这三个方法；
// 测试注入假循环，断言启动块被真的执行。
type loopHandle interface {
	Start() error
	Stop(ctx context.Context) (int64, error)
	Err() error
}

// newLoop 是执行循环的构造接缝。删除 Run 的启动块时测试必红：
// 假循环收不到 Start/Stop。
var newLoop = func(stack *Stack, interval time.Duration, limit int) loopHandle {
	return appworker.NewLoop(stack.Worker, stack.Worker, clock.System{},
		stack.EngineState, interval, limit)
}

// NewServer 装配进程但不启动它。
// 测试通过返回的 server 绑定临时端口。
// 每次调用都完整装配一次；生产路径 Run 不调用它（见 Run 的「一次装配」）。
func NewServer(cfg *config.Config, logger *slog.Logger) (*http.Server, error) {
	if logger == nil {
		logger = slog.Default()
	}
	stack, err := buildStack(cfg, logger)
	if err != nil {
		return nil, err
	}
	return newServerFromStack(cfg, stack, true), nil
}

// HTTP 服务器超时预算。
// 写超时是 SSE 清本次连接截止时间前的上限，也是慢客户端被切断的时刻；
// 数值只在常量里出现一次，独立校验见 timeouts_test.go。
const (
	httpReadHeaderTimeout = 5 * time.Second
	httpReadTimeout       = 15 * time.Second
	httpWriteTimeout      = 30 * time.Second
	httpIdleTimeout       = 60 * time.Second
)

// newServerFromStack 用已装配好的 Stack 构造未启动的 server。
//
// closeOnShutdown 决定 HTTP 关闭是否释放连接池：
//   - NewServer 传 true：调用方没有显式收尾，Shutdown 必须释放池。
//   - Run 传 false：池只在 worker 在飞结算完成后由 stopStack 关闭。
//     Shutdown 提前关池会让 loop.Stop 的 CountRunning 报 closed pool，
//     在飞清零失败（2026-10-28 部署 e2e 实测抓到）。
func newServerFromStack(cfg *config.Config, stack *Stack, closeOnShutdown bool) *http.Server {
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           stack.Engine,
		ReadHeaderTimeout: httpReadHeaderTimeout,
		ReadTimeout:       httpReadTimeout,
		WriteTimeout:      httpWriteTimeout,
		IdleTimeout:       httpIdleTimeout,
	}
	if closeOnShutdown {
		srv.RegisterOnShutdown(stack.Close)
	}
	return srv
}

// Build 装配数据库、用例与路由。
//
// 有 DSN 时：迁移 → 连接池 → 仓储 → 认证与全部业务域用例。
// 无 DSN 时：内存会话；认证与业务域端点回 501，并打印警告。
// **不静默降级**：没有数据库就没有可用登录，警告写在启动日志里。
// Build 装配全部服务与真实签到提供方。
func Build(cfg *config.Config, logger *slog.Logger) (*Stack, error) {
	if cfg == nil {
		return nil, errors.New("assembly: nil config")
	}
	if logger == nil {
		logger = slog.Default()
	}
	clk := clock.System{}
	var (
		issuer      auth.SessionIssuer
		authSvc     *appauth.Service
		schools     *schoolhttp.Handler
		admins      *adminhttp.Handler
		accounts    *accounthttp.Handler
		users       *userhttp.Handler
		signing     *signinghttp.Handler
		planning    *schedulehttp.Handler
		taskLib     *schedulehttp.TasksHandler
		engineLib   *schedulehttp.EngineHandler
		photoLib    *photoshttp.Handler
		locLib      *locationhttp.Handler
		eventLib    *eventshttp.Handler
		calendarLib *calendarhttp.Handler
		notifyLib   *notifyhttp.Handler
		systemLib   *systemhttp.Handler
		auditLib    *audithttp.Handler
		settingsSvc *appsettings.Service
		db          *pg.DB
		workerSvc   *appworker.Service
		// authz 只给 master 专属的注册开关用。无数据库时为 nil，端点回 501。
		authz router.Authorizer
		// engineState 是执行循环的进程内时刻记录，DB 分支构造：
		// 循环（Run）写入，执行监控端点读取，两者共享同一实例。
		// 无数据库时保持 nil：没有循环就没有进程时刻。
		engineState *appworker.EngineState
		closeFn     = func() {}
	)

	// 可信代理表同时服务登录来源判定；与限流开关解耦。
	trustedProxies, err := ratelimit.ParseTrustedProxies(cfg.TrustedProxies)
	if err != nil {
		return nil, fmt.Errorf("assembly: trusted proxies: %w", err)
	}
	// 限流面只依赖配置：提前构造，账号处理器与路由共用同一实例。
	guard, err := buildRateLimit(cfg)
	if err != nil {
		return nil, err
	}
	// 测试邮件冷却：每个管理员 60s 一封。复用入站限流器的令牌桶实现，
	// 但独立实例与键前缀：不占用登录/签到等账号限流额度。
	mailTestThrottle, err := buildMailTestThrottle()
	if err != nil {
		return nil, err
	}

	if strings.TrimSpace(cfg.DatabaseURL) == "" {
		issuer = session.NewMemory(clk, cfg.HashPepper)
		logger.Warn("auth endpoints are disabled: YIBAN_DATABASE_URL is empty")
		logger.Warn("user, school, admin and account endpoints return 501: YIBAN_DATABASE_URL is empty")
	} else {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()

		if _, err := pg.Migrate(ctx, cfg.DatabaseURL); err != nil {
			return nil, fmt.Errorf("assembly: migrate: %w", err)
		}
		var err error
		db, err = pg.Open(ctx, pg.Options{DSN: cfg.DatabaseURL})
		if err != nil {
			return nil, fmt.Errorf("assembly: open database: %w", err)
		}
		closeFn = db.Close

		auditKey, err := coreaudit.DeriveKey(cfg.HashPepper)
		if err != nil {
			closeFn()
			return nil, err
		}
		audits, err := pg.NewAuditRepo(db, auditKey)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: audit repo: %w", err)
		}
		// settings 行同时服务注册开关、自动审批开关与设置页写入，
		// 以及 sett2 的调度三键覆盖链（DB>env）。
		settingsRepo := pg.NewSettingsRepo(db)
		// 覆盖链解析器：整型域取自嵌入名册 domain（唯一源），缺域即启动失败。
		// 名册在这里提前解析一次，resolver 域与设置页名册共用同一份条目。
		registryEntries, err := config.Registry()
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: configuration registry: %w", err)
		}
		settingsResolver, err := buildSettingsResolver(settingsRepo, registryEntries, logger)
		if err != nil {
			closeFn()
			return nil, err
		}
		// 凭据钥独立于 pepper：轮换 pepper 只让摘要与审计链失效，
		// 不能连带让凭据永久不可解密。mail.pass 复用同一把钥加密落库。
		cipher, err := appaccount.NewCipher(cfg.CredentialKey)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: credential cipher: %w", err)
		}
		// mail 六键的覆盖链解析器：GET 回显与每次 SMTP 外呼共用同一实例，
		// 坏行 Warn 一次的口径只有一份。env 兜底值来自 config.Load 的定型值。
		mailResolver := appsettings.NewMailResolver(settingsRepo, cipher,
			mailEnvFromConfig(cfg, config.Env{}), logger)
		// 设置面：订阅、公告、名册、测试邮件与审计列表。
		// 名册缺失只让 GET /api/admin/settings 回 501：其余端点不依赖它。
		// 邮件端口先于设置面构造：测试邮件与忘记口令共用同一实现。
		// 关闭时留显性启动警告；打开但配置不全由 config.Load 拒绝。
		// 只看 YIBAN_RESET_MAIL_ENABLE：YIBAN_MAIL_ENABLE 属旧通知线，Go 侧不读。
		// 日志只记 host/port/tls_mode：用户名与口令是敏感值，不得出现。
		var mailer coremail.Sender
		if cfg.ResetMailEnabled {
			sender, err := mailadapter.NewSMTP(mailadapter.Config{
				// 每发送解析：设置页写回的下一次外呼即生效，无需重启。
				// 信任根/超时/时基仍取静态配置，不随解析变化。
				Resolve: func(ctx context.Context) (mailadapter.Settings, error) {
					_, mailCfg, err := mailResolver.Resolve(ctx)
					if err != nil {
						return mailadapter.Settings{}, err
					}
					return mailadapter.Settings{
						Host:     mailCfg.Host,
						Port:     mailCfg.Port,
						Username: mailCfg.User,
						Password: mailCfg.Pass,
						From:     mailCfg.From,
						TLSMode:  mailCfg.TLSMode,
					}, nil
				},
				TLSConfig: cfg.MailTLSConfig,
				Clock:     clk,
			})
			if err != nil {
				closeFn()
				return nil, fmt.Errorf("assembly: mail sender: %w", err)
			}
			mailer = sender
			logger.Info("mail: SMTP delivery enabled",
				logfield.KeyHost, cfg.MailHost, logfield.KeyPort, cfg.MailPort, logfield.KeyTLSMode, cfg.MailTLSMode)
		} else {
			// 通知邮件与忘记口令、测试邮件共用同一 SMTP 端口与同一开关；
			// 关掉时通知账本行落 skipped(smtp_not_configured)，不静默丢弃。
			logger.Warn("mail: SMTP delivery is disabled; password reset links, test mail and notification mail are not delivered",
				logfield.KeyReason, "YIBAN_RESET_MAIL_ENABLE is false")
		}
		// webhook 推送出口先于设置面构造：测试推送与通知投递共用同一实现。
		// 开关关闭或密钥为空都按未配置：通知账本行落
		// skipped(webhook_not_configured)，测试推送回 409。
		// 日志只记出口类型：Secret 是 SendKey 或完整 URL，不得出现。
		var webhookSender corewebhook.Sender
		if cfg.WebhookEnable && cfg.WebhookSecret != "" {
			sender, err := webhookadapter.New(webhookadapter.Config{
				Type:   cfg.WebhookType,
				Secret: cfg.WebhookSecret,
			})
			if err != nil {
				closeFn()
				return nil, fmt.Errorf("assembly: webhook sender: %w", err)
			}
			webhookSender = sender
			logger.Info("webhook: push delivery enabled",
				logfield.KeyReason, cfg.WebhookType)
		} else {
			logger.Warn("webhook: push delivery is disabled; webhook deliveries are not sent",
				logfield.KeyReason, "YIBAN_WEBHOOK_ENABLE is false or YIBAN_WEBHOOK_SECRET is empty")
		}
		// 测试推送与测试邮件同值 60s：独立实例不占邮件冷却额度。
		webhookTestThrottle, err := buildWebhookTestThrottle()
		if err != nil {
			closeFn()
			return nil, err
		}
		notifyLib, systemLib, auditLib, settingsSvc, err = buildSettings(db, audits, clk, logger, mailer, mailTestThrottle,
			webhookSender, webhookTestThrottle, settingsRepo, settingsRepo, settingsResolver, mailResolver, cipher)
		if err != nil {
			closeFn()
			return nil, err
		}
		sessionRepo := pg.NewSessionRepo(db)
		issuer, err = pg.NewSessionIssuer(sessionRepo, audits, cfg.HashPepper, clk)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: session issuer: %w", err)
		}
		// 一个 KDF 闸服务全部用例：并发上限按进程计，不是按服务计。
		kdf := auth.NewBounded(cfg.KDFConcurrency)
		authSvc, err = appauth.NewService(appauth.Deps{
			Users:         pg.NewUserRepo(db),
			Sessions:      issuer,
			OtherSessions: sessionRepo,
			Resets:        pg.NewPasswordResetRepo(db),
			Registrations: settingsRepo,
			Audits:        audits,
			Tx:            db,
			Clock:         clk,
			Quota:         settingsResolver,
			KDF:           kdf,
			Mail:          mailer,
			MailBaseURL:   cfg.PublicBaseURL,
			Logger:        logger,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: auth service: %w", err)
		}
		// SMTP 设置写面要求二次口令：系统设置处理器与账号/用户/管理员
		// 三个危险操作面共用同一个 authSvc 窄接口（只暴露 Reauthenticate）。
		if systemLib != nil {
			systemLib.WithReauth(authSvc)
		}
		// 关闭顺序：先等在途投递（有上界），再关池。
		closeAuth := closeFn
		closeFn = func() {
			ctx, cancel := context.WithTimeout(context.Background(), shutdownBudget)
			_ = authSvc.Close(ctx)
			cancel()
			closeAuth()
		}

		schoolRepo := pg.NewSchoolRepo(db)
		schoolSvc, err := appschool.NewService(appschool.Deps{
			Schools: schoolRepo,
			Audit:   audits,
			Tx:      db,
			Clock:   clk,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: school service: %w", err)
		}
		// adminSvc 同时是三个域的作用域解析器：
		// 入口层只认一个 Authorizer，故绑定校口径只有一份。
		adminSvc, err := appadmin.NewService(appadmin.Deps{
			Admins:  pg.NewAdminSchoolRepo(db),
			Users:   pg.NewUserRepo(db),
			Schools: schoolRepo,
			Audit:   audits,
			Tx:      db,
			Clock:   clk,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: admin service: %w", err)
		}
		authz = adminSvc
		// 用户仓储先于账号服务构造：账号用例的属主活跃校验复用同一实例
		//（仓储无状态）。Sessions 是会话撤销原语：重置口令与软删后立即
		// 失效全部会话。OwnerAccounts 用同一份用户仓储：名下账号计数是
		// 它的一条只读 SQL。
		userRepo := pg.NewUserRepo(db)
		// 通知投递：三个管理员事件与用户本人失败邮件。SMTP 与忘记口令
		// 共用同一端口，也共用 YIBAN_RESET_MAIL_ENABLE 开关；未配置时
		// 通知账本行落 skipped(smtp_not_configured)，不静默丢弃。
		// webhook 端口独立开关（YIBAN_WEBHOOK_ENABLE），未配置时账本行
		// 落 skipped(webhook_not_configured)。同题节流与日额度参数来自
		// 同一组环境键；额度单源在账本本身。业务日时区与摘要同源。
		// 投递端口先于账号服务构造：ReviewAlerts 在 account 构造时注入。
		// 摘要端口是同一仓储的可选面：每日摘要与事件投递共用同一
		// dispatcher 与邮件账本，但收件人与去重键各自判定。
		loc, err := clock.LoadLocation(cfg.Timezone)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: load timezone %q: %w", cfg.Timezone, err)
		}
		digestRepo := pg.NewNotificationDigestRepo(db)
		notificationDispatcher, err := appnotify.NewDispatcher(appnotify.DispatcherDeps{
			Notes:           pg.NewNotificationRepo(db),
			Ledger:          pg.NewNotificationDeliveryRepo(db),
			Audience:        pg.NewNotificationAudienceRepo(db),
			Mail:            mailer,
			Webhook:         webhookSender,
			WebhookCooldown: time.Duration(cfg.WebhookCooldownSec) * time.Second,
			WebhookDailyMax: cfg.WebhookDailyMax,
			Location:        loc,
			Clock:           clk,
			Logger:          logger,
			DigestSource:    digestRepo,
			DigestAudience:  digestRepo,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: notification dispatcher: %w", err)
		}
		// 一个 provider 同时服务"凭据校验"、"设备刷新"与"执行循环"：
		// 三者必须走同一六请求链实现，避免校验用一套、签到用另一套的漂移。
		provider := yiban.NewProvider(yiban.New(yiban.Config{Clock: clk}))
		// 出口分配与限速器先于交互面装配：worker 与交互面共用同一实例，
		// 交互端点因此与执行循环同闸同预算（批 abs R1 / F2）。
		egressSvc, err := appegress.NewService(appegress.Deps{
			Store: pg.NewEgressRepo(db), Clock: clk,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: egress service: %w", err)
		}
		egressLimiter := buildEgressGate(cfg)
		// 急停门只此一个闭包：worker Step 与交互面平台调用读同一口径。
		// 坏行 fail-closed（视为暂停），见 appsettings.Resolver.ResolveEnginePaused。
		pausedGate := func(ctx context.Context) (bool, error) {
			return settingsResolver.ResolveEnginePaused(ctx)
		}
		platformGate := newEgressGate(egressSvc, egressLimiter, clk, pausedGate)
		if platformGate == nil {
			closeFn()
			return nil, errors.New("assembly: platform egress gate is not wired")
		}
		// 交互面 provider：detect/verify/person-name/device-refresh/
		// auto-approve/窗口发现都经它，先过急停与出口闸再发平台请求。
		// worker 仍收裸 provider：它有自己的 Step 门与限速，避免双重排队。
		gated := gatedProvider{inner: provider, gate: platformGate}
		accountSvc, err := appaccount.NewService(appaccount.Deps{
			Accounts:         pg.NewAccountRepo(db),
			Schools:          schoolRepo,
			Audit:            audits,
			Tx:               db,
			Clock:            clk,
			Cipher:           cipher,
			Owner:            userRepo,
			Device:           accountDeviceReader{provider: gated},
			Settings:         settingsRepo,
			Identity:         gated,
			SchoolProjection: schoolSvc,
			Quota:            settingsResolver,
			ReviewAlerts:     notificationDispatcher,
			Logger:           logger,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: account service: %w", err)
		}
		// 关闭顺序：先取消并等待在飞自动核验，再关池。
		closeAccount := closeFn
		closeFn = func() {
			ctx, cancel := context.WithTimeout(context.Background(), shutdownBudget)
			accountSvc.Shutdown(ctx)
			cancel()
			closeAccount()
		}
		userSvc, err := appuser.NewService(appuser.Deps{
			Users:         userRepo,
			Audit:         audits,
			Tx:            db,
			Clock:         clk,
			KDF:           kdf,
			Quota:         settingsResolver,
			Sessions:      issuer,
			OwnerAccounts: userRepo,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: user service: %w", err)
		}
		schools = schoolhttp.NewHandler(schoolSvc, adminSvc)
		admins = adminhttp.NewHandler(adminSvc, adminSvc).WithReauth(authSvc)
		// 二次口令用例注入三个危险操作面：改角色/删用户/重置口令/恢复/新建、
		// 凭据编辑/删号/自助删号/设备刷新/恢复、学校管理员绑定/解绑。
		// 用例是 authSvc 的窄接口，handler 只见 Reauthenticate。
		accounts = accounthttp.NewHandler(accountSvc, adminSvc, guard).WithReauth(authSvc)
		users = userhttp.NewHandler(userSvc, adminSvc).WithReauth(authSvc)
		// 姓名揭示审计与凭据读取是同一个账号用例实例：揭示行由它追加。
		// verifier 用交互面 gated provider：person-name 与 verify 同过急停/出口闸。
		signing = signinghttp.NewHandler(accountSvc, adminSvc, gated).WithRevealAudit(accountSvc)

		// 事件总线先于 worker 与计划装配：两者的发布端口引用同一实例。
		broker := appevents.NewBroker()
		// 执行循环：手动签到入队 + 进程启动时回收无租约或租约过期的 running 行。
		// 出口服务与限速器已在交互面之前装配，两者共用同一实例。
		// 图片库先于执行循环装配：worker 的选图端口引用同一实例。
		// loc 已在通知投递装配处加载（首个使用者），此处直接复用。
		blobs, err := storage.NewFS(cfg.PhotoDir)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: photo storage: %w", err)
		}
		// 同一仓储服务图片库与学校默认图：默认图登记在 schools.default_photo_id。
		photoRepo := pg.NewPhotoRepo(db)
		photoSvc, err := appphotos.NewService(appphotos.Deps{
			Store: photoRepo, Blobs: blobs, Clock: clk,
			Audit: audits,
			Tx:    db,
			Options: appphotos.Options{
				MaxPixels: photoMaxPixels, MaxSidePx: photoMaxSidePx,
				// 随机裁切缺省关闭，配置显式开启后才裁。
				Crop: cfg.PhotoCrop,
				// 解码闸按进程计：图片入口只有这一个服务。
				Decode: coreimage.NewGate(coreimage.DecodeConcurrency),
			},
			Accounts:     accountSvc,
			SchoolPhotos: photoRepo,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: photo service: %w", err)
		}
		// 保留期清理的生产调用者：启动即一轮，之后每 24h 一轮。
		// 用独立 context：Build 的超时 context 在返回时取消，会把循环一起停掉。
		// 关闭路径先停循环再关池；Stack.Close 已包 sync.Once，重复调用安全。
		stopRetention, err := appphotos.StartRetention(context.Background(), photoSvc, logger)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: retention loop: %w", err)
		}
		closeDB := closeFn
		closeFn = func() {
			stopRetention()
			closeDB()
		}
		// 过期数据清理：会话、重置令牌、90 天前的任务行。
		// 与图片保留期分开：一个是用户策略，一个是表大小。
		retentionSvc, err := appretention.NewService(appretention.Deps{
			Store:    pg.NewMaintenanceRepo(db),
			Clock:    clk,
			Location: loc,
			Logger:   logger,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: retention service: %w", err)
		}
		stopDataRetention := appretention.Start(context.Background(), retentionSvc, logger)
		beforeRetention := closeFn
		closeFn = func() {
			stopDataRetention()
			beforeRetention()
		}
		// 通知邮件的重试循环：启动即跑一轮，之后每 DeliveryInterval 一轮。
		// 独立 context：Build 的超时 context 在返回时取消，会把循环一起停掉。
		stopNotifyDeliveries := appnotify.Start(context.Background(), notificationDispatcher, logger)
		beforeNotify := closeFn
		closeFn = func() {
			stopNotifyDeliveries()
			beforeNotify()
		}
		locationSvc, err := applocation.NewService(applocation.Deps{
			Store:    pg.NewLocationRepo(db),
			Accounts: accountSvc,
			Audit:    audits,
			Tx:       db,
			Clock:    clk,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: location service: %w", err)
		}
		// 排期仓储同时是 worker 的 Store：凭据错结算要写账号状态与引擎审计，
		// 故注入审计仓储。缺它时结算报错（不静默跳过审计）。
		scheduleRepo := pg.NewScheduleRepoWithAudit(db, audits).WithLease(cfg.TaskLease)
		workerSvc, err = appworker.NewService(buildWorkerDeps(cfg, workerDeps{
			store:    scheduleRepo,
			creds:    workerCredentials{svc: accountSvc, kid: cipher.KID()},
			signer:   provider,
			egress:   egressSvc,
			clock:    clk,
			location: loc,
			// P5-C 注入面：定位策略与选图。缺端口时对应面退回现行为。
			locations: locationSvc,
			photos:    photoSvc,
			// P6 注入面：任务事件发布。缺端口时不发事件。
			notifier: WorkerNotifier{Broker: broker, Clock: clk},
			// 0v8 B 注入面：结算后的通知投递。缺端口时不投递。
			alerts: notificationDispatcher,
			// 手动签到多候选任务随机选点。
			picker: appworker.NewRandomTaskPicker(),
			// 结论原因进日志（OCR M1）。
			logger: logger,
			// 3a 全局暂停门：每轮 Step 前查 settings 行 engine.paused 的
			// 覆盖链生效值（DB>缺行 false，坏行 fail-closed）。
			// 与交互面 gated provider 共用同一闭包。
			paused: pausedGate,
			// 与交互面共用同一限速器实例：同闸同预算（批 abs R1 / F2）。
			limiter: egressLimiter,
		}))
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: worker service: %w", err)
		}
		if n, recErr := workerSvc.Recover(ctx); recErr != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: recover orphan tasks: %w", recErr)
		} else if n > 0 {
			logger.Info("worker: recovered orphan running rows", logfield.KeyCount, n)
		}
		signing.WithManual(workerSvc)
		// 手动签到的可见面判据：user 只能签自己的账号，
		// admin 只能签绑定学校的账号（跨校回 404）。
		signing.WithAccess(accountSvc)
		// 批量签到的账号键限流：与单条端点共用同一 Guard，
		// 逐项判定，命中项回 rate_limited 不拖垮整批。
		signing.WithAccountLimiter(guard)

		photoLib = photoshttp.NewHandler(photoSvc, adminSvc)
		locLib = locationhttp.NewHandler(locationSvc, adminSvc)

		prefRepo := pg.NewTimePrefRepo(db)
		// 标准窗口对的每请求解析（DB 覆盖行>core 常量；半对整对回退）。
		// **单一闭包**同时喂计划 fallback 与自选展示窗（3b 复审 F1：
		// 默认窗可写回后，两处必须同源，不得各持一份常量）。
		defaultWindowPair := func(ctx context.Context) (int, int, error) {
			return settingsResolver.ResolveSignWindow(ctx,
				coreschedule.DefaultSignWindowStartMinute, coreschedule.DefaultSignWindowEndMinute)
		}
		scheduleSvc, err := appschedule.NewService(appschedule.Deps{
			Windows:  scheduleRepo,
			Plans:    scheduleRepo,
			Accounts: scheduleRepo,
			Discover: scheduleDiscoverer{provider: gated, accounts: accountSvc, tasks: scheduleRepo},
			Prefs:    prefRepo,
			// 用户自选读面的当日账号/任务解析：与计划仓储同源。
			Self: scheduleRepo,
			Tx:   db,
			// P6 注入面：计划事件发布。缺端口时不发事件。
			Notifier: ScheduleNotifier{Broker: broker, Clock: clk},
			// 0v8 B 注入面：容量缺口通知。缺端口时不投递。
			CapacityAlerts: notificationDispatcher,
			// 3b 注入面：fallback 窗口的全局默认值每请求解析
			//（DB 覆盖行>core 常量；半对整对回退）。缺端口时行为与常量一致。
			Defaults: defaultWindowPair,
			// 显式手动面写库前判急停：与 worker/交互面同一闭包。
			Paused: pausedGate,
			// 窗口发现失败的原因进日志（OCR M1）。
			Logger: logger,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: schedule service: %w", err)
		}
		prefSvc, err := apptimepref.NewService(apptimepref.Deps{
			Store: prefRepo, Clock: clk,
			Allow: func(ctx context.Context) (bool, error) {
				// sett2：自选开关每请求读 DB 覆盖行，行缺失回 env 值。
				return settingsResolver.ResolveBool(ctx, appsettings.KeyScheduleAllowTimePref, cfg.AllowTimePref)
			},
			// 3b F1：展示窗与计划 fallback 窗共用同一解析闭包（同源防漂移）。
			Window:   defaultWindowPair,
			Cooldown: cfg.TimePrefCooldown,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: time preference service: %w", err)
		}
		statsSvc, err := appstats.NewService(pg.NewStatsRepo(db))
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: stats service: %w", err)
		}
		// 每片槽数取一个 5 分钟片的原始吞吐（util=1）。
		// 与容量公式同源：容量只是把全窗吞吐再乘利用系数。
		slots := corecapacity.Capacity(coreplan.SlotSpan, cfg.Workers, cfg.AvgAttempt, cfg.EgressRate, 1.0)
		if slots < 1 {
			slots = 1
		}
		planning = schedulehttp.NewHandler(scheduleSvc, prefSvc, adminSvc, schedulehttp.Config{
			Location: loc, Clock: clk, Workers: cfg.Workers, Avg: cfg.AvgAttempt,
			Rate: cfg.EgressRate, Util: corecapacity.UtilDefault,
			// SignDist 不再持静态副本：每请求经 Overrides 读 DB（缺行=front）。
			SlotsPerSlice: slots, UsePrefs: cfg.AllowTimePref,
			EdgeFront: cfg.EdgeFront, EdgeBack: cfg.EdgeBack,
			// sett2/3c：可写回键每请求解析（DB 覆盖 env）；静态值仍是 env 兜底。
			Overrides: settingsOverrides{resolver: settingsResolver, cfg: cfg},
			Logger:    logger,
		}).WithStats(statsSvc)
		// 运行页任务检索：只读，与计划用例共用排期仓储。
		taskLib = schedulehttp.NewTasksHandler(
			appschedule.NewTaskQuery(scheduleRepo, clk, loc), adminSvc,
			schedulehttp.Config{Location: loc, Clock: clk})
		// 执行监控：快照用例与循环进程时刻同一实例（UX-5）。
		// 3a：快照带全局暂停态（settings 行 engine.paused），写面挂二次口令
		// （暂停 master/admin、恢复仅 master；写与审计同事务在用例层）。
		engineState = appworker.NewEngineState()
		engineLib = schedulehttp.NewEngineHandler(
			appschedule.NewEngineQuery(scheduleRepo, clk, loc), engineState, adminSvc).
			WithPauseGate(settingsSvc, authSvc)
		// 日切构建：启动补建 + 业务日 00:10 定时。参数与 HTTP 建计划同源。
		daily, err := appschedule.NewDailyCron(&appschedule.DailyBuilder{
			Service: scheduleSvc, Tasks: scheduleRepo, Clock: clk, Location: loc,
			Params: appschedule.BuildParams{
				Workers: cfg.Workers, Avg: cfg.AvgAttempt, Rate: cfg.EgressRate,
				Util: corecapacity.UtilDefault, SlotsPerSlice: slots, SignDist: appschedule.SignDistFront,
				UsePrefs: cfg.AllowTimePref, EdgeFront: cfg.EdgeFront, EdgeBack: cfg.EdgeBack,
			},
			// sett2：日切构建每次重读覆盖行；当日已铺行不回改，改值次日生效。
			Overrides: func(ctx context.Context) (appschedule.BuildOverrides, error) {
				return buildScheduleOverrides(ctx, settingsResolver, cfg)
			},
			// 3b：周末门关闭时如实记一行（不铺行不留日志就没有任何痕迹）。
			Logger: logger,
		}, logger)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: daily build cron: %w", err)
		}
		if err := daily.Start(); err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: start daily build cron: %w", err)
		}
		prevClose := closeFn
		closeFn = func() {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			_ = daily.Stop(ctx)
			cancel()
			prevClose()
		}
		// 每日签到失败摘要：业务日 09:00 发上一日，启动补发一次。
		// 与日切构建互不共享状态；摘要只读 sign_tasks 的 failed 终态。
		digestCron, err := appnotify.NewDigestCron(notificationDispatcher, clk, loc, logger)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: digest cron: %w", err)
		}
		if err := digestCron.Start(); err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: start digest cron: %w", err)
		}
		prevDigestClose := closeFn
		closeFn = func() {
			ctx, cancel := context.WithTimeout(context.Background(), shutdownBudget)
			_ = digestCron.Stop(ctx)
			cancel()
			prevDigestClose()
		}
		diagSvc, err := appdiagnostic.NewService(appdiagnostic.Deps{
			Accounts: accountSvc,
			Store:    pg.NewDiagnosticRepo(db),
			Clock:    clk,
			Location: loc,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: diagnostic service: %w", err)
		}
		cookieName := auth.CookieName
		if cfg.InsecureCookies {
			cookieName = auth.InsecureCookieName
		}
		eventLib = eventshttp.NewHandler(broker, diagSvc, adminSvc, clk, nil).
			WithSessionResolver(issuer, cookieName)
		calendarSvc, err := appcalendar.NewService(appcalendar.Deps{
			Store:    pg.NewCalendarRepo(db),
			Clock:    clk,
			Location: loc,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: sign calendar service: %w", err)
		}
		calendarLib = calendarhttp.NewHandler(calendarSvc, adminSvc)
	}

	engine, err := router.New(router.Options{
		Sessions:        issuer,
		Auth:            authSvc,
		Authorizer:      authz,
		Clock:           clk,
		Logger:          logger,
		InsecureCookies: cfg.InsecureCookies,
		Schools:         schools,
		Admins:          admins,
		Accounts:        accounts,
		Users:           users,
		Signing:         signing,
		Schedule:        planning,
		Tasks:           taskLib,
		Engine:          engineLib,
		Photos:          photoLib,
		Locations:       locLib,
		Events:          eventLib,
		Calendar:        calendarLib,
		Notify:          notifyLib,
		System:          systemLib,
		Audit:           auditLib,
		RateLimit:       guard,
		LoginOrigin:     publicOrigin(cfg.PublicBaseURL),
		TrustedProxies:  trustedProxies,
		UIDir:           cfg.UIDir,
		DocsDir:         cfg.DocsDir,
	})
	if err != nil {
		closeFn()
		return nil, err
	}
	// Close 幂等：srv.Shutdown 触发一次，Run 的收尾再显式调一次。
	var closeOnce sync.Once
	closeAll := func() { closeOnce.Do(closeFn) }
	return &Stack{Engine: engine, DB: db, Worker: workerSvc, EngineState: engineState, Close: closeAll}, nil
}

// buildSettings 装配通知订阅、站内信、公告、设置名册与审计只读列表。
//
// 名册随二进制嵌入（internal/configdata），不存在“文件缺失”的降级路径：
// 嵌入名册损坏是构建缺陷，一律失败关闭。带病的名册会把错误的信息
// 展示成配置事实。
func buildSettings(db *pg.DB, audits *pg.AuditRepo, clk clock.Clock,
	logger *slog.Logger, mailer coremail.Sender, cooldown appsettings.Cooldown,
	webhook corewebhook.Sender, webhookCooldown appsettings.Cooldown,
	autoApprove appsettings.AutoApproveStore, settingsRepo *pg.SettingsRepo,
	settingsResolver *appsettings.Resolver, mailResolver *appsettings.MailResolver,
	cipher *appaccount.Cipher) (*notifyhttp.Handler, *systemhttp.Handler, *audithttp.Handler, *appsettings.Service, error) {
	notifySvc, err := appnotify.NewService(pg.NewNotificationPrefsRepo(db))
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("assembly: notify service: %w", err)
	}
	inboxSvc, err := appnotify.NewInbox(appnotify.InboxDeps{
		Repo:  pg.NewNotificationRepo(db),
		Clock: clk,
	})
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("assembly: notification inbox: %w", err)
	}
	announcementSvc, err := appsettings.NewService(appsettings.Deps{
		Store:           pg.NewAnnouncementRepo(db),
		Audit:           audits,
		Tx:              db,
		Clock:           clk,
		Mail:            mailer,
		Webhook:         webhook,
		Cooldown:        cooldown,
		WebhookCooldown: webhookCooldown,
		AutoApprove:     autoApprove,
		Schedule:        settingsRepo,
		Resolver:        settingsResolver,
		Cipher:          cipher,
		MailResolver:    mailResolver,
		Logger:          logger,
	})
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("assembly: announcement service: %w", err)
	}
	reader, err := appaudit.NewReader(audits)
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("assembly: audit reader: %w", err)
	}
	// 巡检复用同一个 AuditRepo：适配器持有 HMAC 密钥，用例只拿结论。
	inspector, err := appaudit.NewInspector(audits, clk)
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("assembly: audit inspector: %w", err)
	}

	entries, err := config.Registry()
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("assembly: configuration registry: %w", err)
	}
	roster, err := appsettings.NewRoster(rosterEntries(entries), config.Env{})
	if err != nil {
		return nil, nil, nil, nil, fmt.Errorf("assembly: configuration roster: %w", err)
	}
	// sett2：名册接覆盖链，写回键在 DB 行存在时报 source=db。
	roster.WithOverrides(settingsResolver)
	notifyHandler := notifyhttp.NewHandler(notifySvc).WithInbox(inboxSvc)
	systemHandler := systemhttp.NewHandler(roster, announcementSvc, announcementSvc).
		WithAutoApprove(announcementSvc).
		WithWebhookTest(announcementSvc).
		WithScheduleSettings(announcementSvc).
		WithQuotaSettings(announcementSvc).
		WithMailSettings(announcementSvc)
	return notifyHandler, systemHandler, audithttp.NewHandler(reader).WithStatus(inspector), announcementSvc, nil
}

// buildWebhookTestThrottle 构造测试推送的每管理员冷却。
// 与测试邮件同值 60s、同形态：60s = 1/min + burst 1，独立实例不占邮件额度。
func buildWebhookTestThrottle() (*ratelimit.Throttle, error) {
	throttle, err := ratelimit.NewThrottle(ratelimit.Quota{
		PerMinute: int(time.Minute / appsettings.WebhookTestCooldown),
		Burst:     1,
	})
	if err != nil {
		return nil, fmt.Errorf("assembly: webhook test throttle: %w", err)
	}
	return throttle, nil
}

// rosterEntries 把适配器的名册条目转成用例视图。
// 只放行 scope=server：设置页展示的是服务进程的配置面；
// scope=cli 的键（backup 口令、迁移旧钥、bench DSN）不属于它，
// 列出来等于把运维工具的密钥位暴露给管理员界面。
func rosterEntries(entries []config.RegistryEntry) []appsettings.Entry {
	out := make([]appsettings.Entry, 0, len(entries))
	for _, entry := range entries {
		if entry.Scope != config.ScopeServer {
			continue
		}
		out = append(out, appsettings.Entry{
			Key:        entry.Key,
			Group:      entry.Group,
			Sensitive:  entry.Sensitive,
			Default:    entry.Default,
			HasDefault: entry.HasDefault,
		})
	}
	return out
}

// publicOrigin 把公开基址收敛成 scheme://host[:port]。
// 解析失败返回空串：登录端点退回同源判定，不静默放行跨站。
func publicOrigin(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme == "" || u.Host == "" {
		return ""
	}
	return u.Scheme + "://" + u.Host
}

// buildRateLimit 构造入站限流面。
// 关闭开关时返回 nil：路由层把 nil Guard 视为不限流。
// 可信代理表非法即启动失败：静默忽略会让限流按代理 IP 计数。
func buildRateLimit(cfg *config.Config) (*ratelimit.Guard, error) {
	if !cfg.RateLimitEnabled {
		return nil, nil
	}
	trusted, err := ratelimit.ParseTrustedProxies(cfg.TrustedProxies)
	if err != nil {
		return nil, fmt.Errorf("assembly: trusted proxies: %w", err)
	}
	ip, err := ratelimit.NewThrottle(ratelimit.Quota{
		PerMinute: cfg.RateIPPerMin, Burst: cfg.RateIPBurst,
	})
	if err != nil {
		return nil, fmt.Errorf("assembly: ip rate limit: %w", err)
	}
	account, err := ratelimit.NewThrottle(ratelimit.Quota{
		PerMinute: cfg.RateAccountPerMin, Burst: cfg.RateAccountBurst,
	})
	if err != nil {
		return nil, fmt.Errorf("assembly: account rate limit: %w", err)
	}
	return &ratelimit.Guard{IP: ip, Account: account, Trusted: trusted}, nil
}

// buildMailTestThrottle 构造测试邮件的每管理员冷却。
// 60s 冷却 = 1/min + burst 1：burst 若大于 1，窗口开头可连发，
// 与「每管理员 60s 一封」的口径不符。独立实例，不占账号限流额度。
func buildMailTestThrottle() (*ratelimit.Throttle, error) {
	throttle, err := ratelimit.NewThrottle(ratelimit.Quota{
		PerMinute: int(time.Minute / appsettings.TestMailCooldown),
		Burst:     1,
	})
	if err != nil {
		return nil, fmt.Errorf("assembly: mail test throttle: %w", err)
	}
	return throttle, nil
}

// egressBurst 是每出口令牌桶的突发深度（账号尝试数）。
// 它只吸收短时抖动，不改长期速率：长期速率由 YIBAN_EGRESS_RATE 决定。
const egressBurst = 6

// workerDeps 是执行循环除限速器之外的依赖。
// 单列出来是为了让 buildWorkerDeps 可以脱离装配被测试：
// 限速器的速率必须与计划容量公式同源，判据要咬住接线那一行。
// limiter 是可选共享实例：装配层传入交互面同款限速器（同闸同预算）；
// 为空时 buildWorkerDeps 按配置派生，保持单测可独立构造。
type workerDeps struct {
	store     appworker.Store
	creds     appworker.Credentials
	signer    appworker.Signer
	egress    appworker.Egress
	clock     clock.Clock
	location  *time.Location
	locations appworker.LocationSource
	photos    appworker.PhotoSource
	notifier  appworker.Notifier
	alerts    appworker.Alerts
	picker    appworker.TaskPicker
	logger    *slog.Logger
	paused    func(ctx context.Context) (bool, error)
	limiter   *corerate.Limiter
}

// buildWorkerDeps 装配执行循环的依赖，限速器默认由 buildEgressGate 派生。
//
// 速率必须与计划容量公式取**同一字段**：两处不同源时，缺口门会用
// 调大的 YIBAN_EGRESS_RATE 放行一份执行面跑不完的计划（OCR C2 P1）。
func buildWorkerDeps(cfg *config.Config, d workerDeps) appworker.Deps {
	limiter := d.limiter
	if limiter == nil {
		limiter = buildEgressGate(cfg)
	}
	return appworker.Deps{
		Store:     d.store,
		Creds:     d.creds,
		Signer:    d.signer,
		Egress:    d.egress,
		Limiter:   limiter,
		Clock:     d.clock,
		Location:  d.location,
		Locations: d.locations,
		Photos:    d.photos,
		Notifier:  d.notifier,
		Alerts:    d.alerts,
		Picker:    d.picker,
		Logger:    d.logger,
		Paused:    d.paused,
	}
}

// buildEgressGate 构造执行面的每出口限速器。
func buildEgressGate(cfg *config.Config) *corerate.Limiter {
	return corerate.NewLimiter(cfg.EgressRate, egressBurst)
}

// workerCredentials 把账号用例的明文凭据适配成 signing 契约的形状。
//
// 组合根适配：只做 app/account 明文凭据 → signing 契约的形状转换。
// 适配只在此一处：app/account 不依赖 signing 契约，worker 也不直接读库。
// 作用域用 master：执行循环是系统动作，不属于任何管理员的可见面。
type workerCredentials struct {
	svc *appaccount.Service
	kid string
}

// Credential 取一次调用内有效的账号凭据。
func (w workerCredentials) Credential(ctx context.Context, accountID string) (contract.Credential, error) {
	c, err := w.svc.SigningCredential(ctx, corescope.Master(), accountID)
	if err != nil {
		return contract.Credential{}, err
	}
	return contract.Credential{
		Phone:      c.Phone,
		Password:   c.Password,
		DeviceCode: c.DeviceCode,
		PhoneModel: c.PhoneModel,
	}, nil
}

// KID 报告当前凭据密钥的单向指纹，供 worker 的放弃行自证"我方解密正常"。
// 只暴露指纹，不是密钥材料：由 Cipher 派生，sha256/HMAC 前 8 字节。
func (w workerCredentials) KID() string { return w.kid }

// accountDeviceReader 把 provider 的设备读回适配成账号用例的设备端口。
//
// 组合根适配：只做 provider 设备读回 → app/account 端口的形状转换。
// 适配只在此一处（同 workerCredentials 口径）：app/account 不依赖
// signing 类型；adapters 不得 import providers，故实现在组合根。
// provider 指针在装配时注入；六个请求链的会话缓存由 provider 自持。
// 注入的是交互面 gated provider：设备读回与认证同过急停/出口闸。
type accountDeviceReader struct {
	provider deviceStateReader
}

var _ appaccount.DeviceReader = accountDeviceReader{}

// DeviceState 读一次平台登记的设备字段。
// 明文凭据只进 provider；返回值不含凭据，错误原样上抛由用例分类。
func (r accountDeviceReader) DeviceState(ctx context.Context, accountID string, cred appaccount.SigningCredential) (appaccount.DeviceState, error) {
	state, err := r.provider.DeviceStateOf(ctx, accountID, contract.Credential{
		Phone:      cred.Phone,
		Password:   cred.Password,
		DeviceCode: cred.DeviceCode,
		PhoneModel: cred.PhoneModel,
	})
	if err != nil {
		return appaccount.DeviceState{}, err
	}
	return appaccount.DeviceState{
		Authorized: state.Authorized,
		Code:       state.Code,
		PhoneModel: state.PhoneModel,
	}, nil
}

// scheduleDiscoverer 用同校一个活跃账号读取平台窗口。
//
// 窗口属于任务，平台调用却需要登录凭据：借首个活跃账号的登录态。
// 账号选择策略在 app/account.FirstActiveForSchool；本类型只做接线：
// 读任务、取凭据、调 provider。该校没有可用账号时返回 not_found，
// 计划层按缓存或回退窗口继续，不把"没有可借用账号"当成平台故障。
type scheduleDiscoverer struct {
	provider windowDiscoverer
	accounts *appaccount.Service
	tasks    *pg.ScheduleRepo
}

// DiscoverWindow 读一个任务当日的平台窗口。
func (d scheduleDiscoverer) DiscoverWindow(ctx context.Context, schoolTaskID string) (contract.Window, error) {
	task, err := d.tasks.Task(ctx, schoolTaskID)
	if err != nil {
		return contract.Window{}, err
	}
	acct, err := d.accounts.FirstActiveForSchool(ctx, task.SchoolID)
	if err != nil {
		return contract.Window{}, err
	}
	cred, err := d.accounts.SigningCredential(ctx, corescope.Master(), acct.ID)
	if err != nil {
		return contract.Window{}, err
	}
	return d.provider.DiscoverWindow(ctx, contract.DiscoverRequest{
		Account: contract.AccountRef{ID: acct.ID, SchoolID: task.SchoolID, Platform: "yiban"},
		Credential: contract.Credential{
			Phone:      cred.Phone,
			Password:   cred.Password,
			DeviceCode: cred.DeviceCode,
			PhoneModel: cred.PhoneModel,
		},
	})
}

// WorkerNotifier 把执行循环的任务事件转成总线事件。
// 组合根适配：只做执行循环事件 → 进程内总线的形状转换。
// Publish 永不阻塞；失败只丢事件。At 取进程时钟。
// 字段导出：e2e 直接构造同形适配器（w2c 套件）。
type WorkerNotifier struct {
	Broker *appevents.Broker
	Clock  clock.Clock
}

// TaskEvent 实现 appworker.Notifier。
func (n WorkerNotifier) TaskEvent(_ context.Context, schoolID, taskID, day, kind string, payload map[string]any) {
	n.Broker.Publish([]string{schoolID}, appevents.Event{
		Type: kind, SchoolID: schoolID, TaskID: taskID, Day: day,
		At: n.Clock.Now(), Payload: payload,
	})
}

// ScheduleNotifier 把计划事件转成总线事件。
// 组合根适配：只做计划事件 → 进程内总线的形状转换。
type ScheduleNotifier struct {
	Broker *appevents.Broker
	Clock  clock.Clock
}

// PlanEvent 实现 appschedule.Notifier。
func (n ScheduleNotifier) PlanEvent(_ context.Context, schoolID, taskID, day, kind string, payload map[string]any) {
	n.Broker.Publish([]string{schoolID}, appevents.Event{
		Type: kind, SchoolID: schoolID, TaskID: taskID, Day: day,
		At: n.Clock.Now(), Payload: payload,
	})
}

// buildSettingsResolver 构造覆盖链解析器（sett2 起、r4r 扩配额两键）。
// 整型域取自嵌入名册 domain（唯一源）：两枚 edge 键缺域即启动失败，
// 写面不得猜域。布尔键无域，只认真小写 true/false 字面。
// 配额两键是 DB-only：不经名册，域由 app/settings 的 QuotaMin/QuotaMax 单源持有。
func buildSettingsResolver(store appsettings.ScheduleStore,
	entries []config.RegistryEntry, logger *slog.Logger) (*appsettings.Resolver, error) {
	// 名册键→行键的映射单源在 app/settings（RegistryKeyAliases）；
	// 这里只把名册 domain 注入到正确的行键上。
	aliases := appsettings.RegistryKeyAliases()
	byKey := make(map[string]config.RegistryEntry, len(entries))
	for _, entry := range entries {
		byKey[entry.Key] = entry
	}
	domains := make(map[string]appsettings.SettingDomain, 2)
	for registryKey, settingsKey := range aliases {
		entry, ok := byKey[registryKey]
		if !ok || entry.Scope != config.ScopeServer {
			return nil, fmt.Errorf("assembly: writable key %s is not a server registry entry", registryKey)
		}
		if settingsKey == appsettings.KeyScheduleAllowTimePref {
			continue // 布尔键无域：字面判定在读取端口内
		}
		if len(entry.Domain) != 2 {
			return nil, fmt.Errorf("assembly: writable key %s has no registry domain", registryKey)
		}
		domains[settingsKey] = appsettings.SettingDomain{
			Min: int(entry.Domain[0]), Max: int(entry.Domain[1]),
		}
	}
	resolver, err := appsettings.NewResolver(store, domains, logger)
	if err != nil {
		return nil, fmt.Errorf("assembly: settings resolver: %w", err)
	}
	return resolver, nil
}

// mailEnvFromConfig 把 config.Load 的定型值转成 mail 六键的 env/registry 兜底视图。
// 来源按「env 是否显式给非空值」判定，与名册口径一致：
// 非空 env=env，否则 default（值仍取 cfg 的定型值，即 registry 缺省）。
// 本函数不读进程环境以外的任何来源；src 由调用方注入（生产是 config.Env）。
func mailEnvFromConfig(cfg *config.Config, src config.Source) appsettings.MailEnv {
	value := func(key, effective string) appsettings.MailEnvValue {
		if raw, ok := src.Lookup(key); ok && raw != "" {
			return appsettings.MailEnvValue{Value: effective, Source: appsettings.MailSourceEnv}
		}
		return appsettings.MailEnvValue{Value: effective, Source: appsettings.MailSourceDefault}
	}
	return appsettings.MailEnv{
		Host:    value("YIBAN_MAIL_SMTP_HOST", cfg.MailHost),
		Port:    value("YIBAN_MAIL_SMTP_PORT", strconv.Itoa(cfg.MailPort)),
		TLSMode: value("YIBAN_MAIL_TLS_MODE", cfg.MailTLSMode),
		From:    value("YIBAN_MAIL_FROM", cfg.MailFrom),
		User:    value("YIBAN_MAIL_USER", cfg.MailUser),
		Pass:    value("YIBAN_MAIL_PASS", cfg.MailPass),
	}
}

// NewSettingsResolver 构造覆盖链解析器，供 CLI 与装配共用同一构造路径。
// create-master 用它读 quota.max_users（缺行=1）：CLI 与 web 同口径，
// 首装 0 用户建 master 恰好通过，有配额后第二用户拒。
func NewSettingsResolver(store appsettings.ScheduleStore, logger *slog.Logger) (*appsettings.Resolver, error) {
	entries, err := config.Registry()
	if err != nil {
		return nil, fmt.Errorf("assembly: configuration registry: %w", err)
	}
	return buildSettingsResolver(store, entries, logger)
}

// settingsOverrides 把覆盖链解析接到排期消费面（schedulehttp 每请求读）。
type settingsOverrides struct {
	resolver *appsettings.Resolver
	cfg      *config.Config
}

// UsePrefs 解析自选偏好总开关：DB 行存在且合法用行值，否则 env 值。
func (o settingsOverrides) UsePrefs(ctx context.Context) (bool, error) {
	return o.resolver.ResolveBool(ctx, appsettings.KeyScheduleAllowTimePref, o.cfg.AllowTimePref)
}

// Edges 解析窗口两端裁剪秒：DB 行存在且在域内用行值，否则 env 值。
func (o settingsOverrides) Edges(ctx context.Context) (time.Duration, time.Duration, error) {
	front, err := o.resolver.ResolveInt(ctx, appsettings.KeyScheduleEdgeFrontSec, int(o.cfg.EdgeFront/time.Second))
	if err != nil {
		return 0, 0, err
	}
	back, err := o.resolver.ResolveInt(ctx, appsettings.KeyScheduleEdgeBackSec, int(o.cfg.EdgeBack/time.Second))
	if err != nil {
		return 0, 0, err
	}
	return time.Duration(front) * time.Second, time.Duration(back) * time.Second, nil
}

// SignDist 解析铺点分布档：DB 行存在且在词表内用行值，否则缺省 front。
// 与日切构建同一解析口径（buildScheduleOverrides 复用同一映射）。
func (o settingsOverrides) SignDist(ctx context.Context) (appschedule.SignDist, error) {
	value, err := o.resolver.ResolveSignDist(ctx, appsettings.SignDistFront)
	if err != nil {
		return appschedule.SignDistFront, err
	}
	return scheduleSignDistOf(value), nil
}

// WeekendSign 解析某业务日是否允许铺行：周六/周日读对应 DB-only 键，
// 缺行=false（对齐日切构建）；非周末恒真且不读库。
func (o settingsOverrides) WeekendSign(ctx context.Context, day time.Time) (bool, error) {
	switch day.Weekday() {
	case time.Sunday:
		return o.resolver.ResolveBool(ctx, appsettings.KeyScheduleSundaySign, false)
	case time.Saturday:
		return o.resolver.ResolveBool(ctx, appsettings.KeyScheduleSaturdaySign, false)
	default:
		return true, nil
	}
}

// buildScheduleOverrides 组装日切构建的可写回键读数。
// 口径：当日已铺行不回改——改值自下一次构建（次日凌晨 00:10）生效。
// 批 3b 扩周末门两键：DB-only 键无 env 对应位，缺行=false=不签
// （ResolveBool 的 envValue 固定传 false，即键的缺行语义）。
// 批 3c 扩分布档：缺行=SignDistFront（现行默认档，既有 Front=true 同形）。
func buildScheduleOverrides(ctx context.Context, resolver *appsettings.Resolver, cfg *config.Config) (appschedule.BuildOverrides, error) {
	usePrefs, err := resolver.ResolveBool(ctx, appsettings.KeyScheduleAllowTimePref, cfg.AllowTimePref)
	if err != nil {
		return appschedule.BuildOverrides{}, err
	}
	front, err := resolver.ResolveInt(ctx, appsettings.KeyScheduleEdgeFrontSec, int(cfg.EdgeFront/time.Second))
	if err != nil {
		return appschedule.BuildOverrides{}, err
	}
	back, err := resolver.ResolveInt(ctx, appsettings.KeyScheduleEdgeBackSec, int(cfg.EdgeBack/time.Second))
	if err != nil {
		return appschedule.BuildOverrides{}, err
	}
	sundaySign, err := resolver.ResolveBool(ctx, appsettings.KeyScheduleSundaySign, false)
	if err != nil {
		return appschedule.BuildOverrides{}, err
	}
	saturdaySign, err := resolver.ResolveBool(ctx, appsettings.KeyScheduleSaturdaySign, false)
	if err != nil {
		return appschedule.BuildOverrides{}, err
	}
	signDist, err := resolver.ResolveSignDist(ctx, appsettings.SignDistFront)
	if err != nil {
		return appschedule.BuildOverrides{}, err
	}
	return appschedule.BuildOverrides{
		UsePrefs:     usePrefs,
		EdgeFront:    time.Duration(front) * time.Second,
		EdgeBack:     time.Duration(back) * time.Second,
		SundaySign:   sundaySign,
		SaturdaySign: saturdaySign,
		SignDist:     scheduleSignDistOf(signDist),
	}, nil
}

// scheduleSignDistOf 把 settings 的字符串枚举映射成计划用例的档位枚举。
// 映射只此一处（app 目录之间不互相 import）：词表单源在 app/settings，
// 计划用例只认自己的枚举。未知值按默认档 front 处理（读面已保证词表内，
// 这里 fail-safe）。
func scheduleSignDistOf(value appsettings.SignDist) appschedule.SignDist {
	if value == appsettings.SignDistUniform {
		return appschedule.SignDistUniform
	}
	return appschedule.SignDistFront
}
