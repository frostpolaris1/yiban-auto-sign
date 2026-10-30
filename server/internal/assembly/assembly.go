// Package assembly 是 Go 服务端的组合根（composition root）。
//
// # 功能
// 装配配置、数据库、会话签发器、全部业务域与路由，并把结果作为
// Stack 交给调用方；Run 服务到 SIGINT 或 SIGTERM。
// cmd/server 与 cmd/cli 的 server 子命令都经由本包启动进程，
// 两条入口共享同一份装配代码，不复制。
// 本包**不**解析命令行参数、**不**打印用法、**不**退出进程。
//
// # 归属取舍（派单 P7 要求写明）
// 候选一：internal/app/bootstrap——被否：canonical.tsv 的 boundary.app
// 禁止 app 层导入 adapters 与 entry，而组合根必须同时导入两者；
// 为它开洞等于削弱既有门。候选二：直接导出 cmd/server 的 Run——
// 不可达：package main 的标识符无法被其他包导入。故新设 internal/assembly：
// structure.tsv 登记为 internal 的子域；canonical.tsv 把 assembly 加进
// 四条 boundary 规则并新增「internal 下不得导入 assembly」，
// 保证它只被 cmd 引用，不成为新的横向依赖通道。
//
// # 复用
// Config、Stack、Build、NewServer、Run、DeriveAuditKey；
// WorkerNotifier、ScheduleNotifier（事件总线适配，测试复用同一形状）。
//
// # 口径
// 优雅关闭上限 10s。
// 无数据库时认证端点不可用，进程打印警告，健康检查仍可服务。
// 有数据库时启动即起执行循环（见 run）：认领到期任务并结算。
package assembly

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	mailadapter "github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/mail"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/session"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/storage"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appadmin "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/admin"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appdiagnostic "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/diagnostic"
	appegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/egress"
	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	applocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/location"
	appphotos "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/photos"
	appschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/schedule"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	apptimepref "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/timepref"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/account"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	corecapacity "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/capacity"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	coremail "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/mail"
	coreplan "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/plan"
	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	adminhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/admin"
	eventshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/events"
	locationhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/location"
	photoshttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/photos"
	ratelimit "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/ratelimit"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	schedulehttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/schedule"
	schoolhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/school"
	signinghttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/signing"
	userhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/providers/yiban"
)

// accountCapacityLimit 是 P1 的活跃账号上限（裁决 12.11）。
// P1 不提供配置项；改上限只改本常量。
const accountCapacityLimit = 10000

// UserCapacityLimit 是 P1 的活跃用户上限（裁决 12.11）；
// CLI 的 create-master 与装配共用同一口径。
// 与账号上限同值，两道约束各自在插入事务内判定。
const UserCapacityLimit = 10000

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
		logger.Info("worker: loop started", "poll_interval", workerPollInterval.String(), "batch", cfg.Workers)
	}

	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()

	select {
	case <-ctx.Done():
		return stopStack(srv, stack, loop, logger)
	case err := <-errc:
		if errors.Is(err, http.ErrServerClosed) {
			stopStack(srv, stack, loop, logger)
			return nil
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
// 顺序判据（派单 P7）：先停 worker 领取，再等在飞结算，
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
			logger.Error("worker: drain bookkeeping failed", "error", loopErr.Error())
		default:
			logger.Info("worker: in-flight drained", "remaining", remain)
		}
		if err := loop.Err(); err != nil {
			logger.Warn("worker: loop recorded a step error before shutdown", "error", err.Error())
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
	Close  func()
}

// buildStack 是 Build 的调用接缝。
// Run 与 NewServer 都经由它；装配次数由测试计数（见 assembly_test.go）。
var buildStack = Build

// loopHandle 是执行循环的启停面。Run 只依赖这三个方法；
// 测试注入假循环，断言启动块被真的执行（派单 P2-2）。
type loopHandle interface {
	Start() error
	Stop(ctx context.Context) (int64, error)
	Err() error
}

// newLoop 是执行循环的构造接缝。删除 Run 的启动块时测试必红：
// 假循环收不到 Start/Stop。
var newLoop = func(stack *Stack, interval time.Duration, limit int) loopHandle {
	return appworker.NewLoop(stack.Worker, stack.Worker, interval, limit)
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
		issuer    auth.SessionIssuer
		authSvc   *appauth.Service
		schools   *schoolhttp.Handler
		admins    *adminhttp.Handler
		accounts  *accounthttp.Handler
		users     *userhttp.Handler
		signing   *signinghttp.Handler
		planning  *schedulehttp.Handler
		photoLib  *photoshttp.Handler
		locLib    *locationhttp.Handler
		eventLib  *eventshttp.Handler
		db        *pg.DB
		workerSvc *appworker.Service
		closeFn   = func() {}
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

		auditKey, err := DeriveAuditKey(cfg.HashPepper)
		if err != nil {
			closeFn()
			return nil, err
		}
		audits, err := pg.NewAuditRepo(db, auditKey)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: audit repo: %w", err)
		}
		sessionRepo := pg.NewSessionRepo(db)
		issuer, err = pg.NewSessionIssuer(sessionRepo, audits, cfg.HashPepper, clk)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: session issuer: %w", err)
		}
		// 邮件端口：关闭时留显性启动警告；打开但配置不全由 config.Load 拒绝。
		// 只看 YIBAN_RESET_MAIL_ENABLE：YIBAN_MAIL_ENABLE 属旧通知线，Go 侧不读。
		// 日志只记 host/port/tls_mode：用户名与口令是敏感值，不得出现。
		var mailer coremail.Sender
		if cfg.ResetMailEnabled {
			sender, err := mailadapter.NewSMTP(mailadapter.Config{
				Host:      cfg.MailHost,
				Port:      cfg.MailPort,
				Username:  cfg.MailUser,
				Password:  cfg.MailPass,
				From:      cfg.MailFrom,
				TLSMode:   cfg.MailTLSMode,
				TLSConfig: cfg.MailTLSConfig,
				Clock:     clk,
			})
			if err != nil {
				closeFn()
				return nil, fmt.Errorf("assembly: mail sender: %w", err)
			}
			mailer = sender
			logger.Info("mail: SMTP delivery enabled",
				"host", cfg.MailHost, "port", cfg.MailPort, "tls_mode", cfg.MailTLSMode)
		} else {
			logger.Warn("mail: SMTP delivery is disabled; password reset links are not delivered",
				"reason", "YIBAN_RESET_MAIL_ENABLE is false")
		}
		// 一个 KDF 闸服务全部用例：并发上限按进程计，不是按服务计。
		kdf := auth.NewBounded(cfg.KDFConcurrency)
		authSvc, err = appauth.NewService(appauth.Deps{
			Users:         pg.NewUserRepo(db),
			Sessions:      issuer,
			OtherSessions: sessionRepo,
			Resets:        pg.NewPasswordResetRepo(db),
			Audits:        audits,
			Tx:            db,
			Clock:         clk,
			KDF:           kdf,
			Mail:          mailer,
			MailBaseURL:   cfg.PublicBaseURL,
			Logger:        logger,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: auth service: %w", err)
		}
		// 关闭顺序：先等在途投递（有上界），再关池。
		closeAuth := closeFn
		closeFn = func() {
			ctx, cancel := context.WithTimeout(context.Background(), shutdownBudget)
			_ = authSvc.Close(ctx)
			cancel()
			closeAuth()
		}

		// 凭据钥独立于 pepper：轮换 pepper 只让摘要与审计链失效，
		// 不能连带让凭据永久不可解密（裁决：W2C-R2-04）。
		cipher, err := appaccount.NewCipher(cfg.CredentialKey)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: credential cipher: %w", err)
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
		accountSvc, err := appaccount.NewService(appaccount.Deps{
			Accounts:   pg.NewAccountRepo(db),
			Schools:    schoolRepo,
			Audit:      audits,
			Tx:         db,
			Clock:      clk,
			Cipher:     cipher,
			Limit:      accountCapacityLimit,
			OwnerLimit: cfg.MaxAccountsPerOwner,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: account service: %w", err)
		}
		// 用户用例复用同一数据库下的用户仓储实例（仓储无状态）：建号走 Create，
		// 它的容量守卫与账号上限同形（裁决 12.11）。
		userRepo := pg.NewUserRepo(db)
		userSvc, err := appuser.NewService(appuser.Deps{
			Users: userRepo,
			Audit: audits,
			Tx:    db,
			Clock: clk,
			KDF:   kdf,
			Limit: UserCapacityLimit,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: user service: %w", err)
		}
		schools = schoolhttp.NewHandler(schoolSvc, adminSvc)
		admins = adminhttp.NewHandler(adminSvc, adminSvc)
		accounts = accounthttp.NewHandler(accountSvc, adminSvc, guard)
		users = userhttp.NewHandler(userSvc, adminSvc)
		// 一个 provider 同时服务"凭据校验"与"执行循环"：两者必须走同一
		// 六请求链实现，避免校验用一套、签到用另一套的漂移。
		provider := yiban.NewProvider(yiban.New(yiban.Config{Clock: clk}))
		signing = signinghttp.NewHandler(accountSvc, adminSvc, provider)

		// 事件总线先于 worker 与计划装配：两者的发布端口引用同一实例。
		broker := appevents.NewBroker()
		// 执行循环：手动签到入队 + 进程启动时回收无租约或租约过期的 running 行。
		// 限速与出口参数暂用模块缺省；配置面（名册键）随 P4-F 收口统一接入。
		egressSvc, err := appegress.NewService(appegress.Deps{
			Store: pg.NewEgressRepo(db), Clock: clk,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: egress service: %w", err)
		}
		loc, err := clock.LoadLocation(cfg.Timezone)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: load timezone %q: %w", cfg.Timezone, err)
		}
		// 图片库先于执行循环装配：worker 的选图端口引用同一实例。
		blobs, err := storage.NewFS(cfg.PhotoDir)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: photo storage: %w", err)
		}
		// 同一仓储服务图片库与学校默认图：默认图登记在 schools.default_photo_id。
		photoRepo := pg.NewPhotoRepo(db)
		photoSvc, err := appphotos.NewService(appphotos.Deps{
			Store: photoRepo, Blobs: blobs, Clock: clk,
			Audit:        audits,
			Tx:           db,
			Options:      appphotos.Options{MaxPixels: photoMaxPixels, MaxSidePx: photoMaxSidePx},
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
			creds:    workerCredentials{svc: accountSvc},
			signer:   provider,
			egress:   egressSvc,
			clock:    clk,
			location: loc,
			// P5-C 注入面：定位策略与选图。缺端口时对应面退回现行为。
			locations: locationSvc,
			photos:    photoSvc,
			// P6 注入面：任务事件发布。缺端口时不发事件。
			notifier: WorkerNotifier{Broker: broker, Clock: clk},
			// 结论原因进日志（OCR M1）。
			logger: logger,
		}))
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: worker service: %w", err)
		}
		if n, recErr := workerSvc.Recover(ctx); recErr != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: recover orphan tasks: %w", recErr)
		} else if n > 0 {
			logger.Info("worker: recovered orphan running rows", "count", n)
		}
		signing.WithManual(workerSvc)
		// 手动签到的可见面判据：user 只能签自己的账号，
		// admin 只能签绑定学校的账号（跨校回 404）。
		signing.WithAccess(accountSvc)

		photoLib = photoshttp.NewHandler(photoSvc, adminSvc)
		locLib = locationhttp.NewHandler(locationSvc, adminSvc)

		prefRepo := pg.NewTimePrefRepo(db)
		scheduleSvc, err := appschedule.NewService(appschedule.Deps{
			Windows:  scheduleRepo,
			Plans:    scheduleRepo,
			Accounts: scheduleRepo,
			Discover: scheduleDiscoverer{provider: provider, accounts: accountSvc, tasks: scheduleRepo},
			Prefs:    prefRepo,
			Tx:       db,
			// P6 注入面：计划事件发布。缺端口时不发事件。
			Notifier: ScheduleNotifier{Broker: broker, Clock: clk},
			// 窗口发现失败的原因进日志（OCR M1）。
			Logger: logger,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: schedule service: %w", err)
		}
		prefSvc, err := apptimepref.NewService(apptimepref.Deps{
			Store: prefRepo, Clock: clk,
			Allow: cfg.AllowTimePref, Cooldown: cfg.TimePrefCooldown,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("assembly: time preference service: %w", err)
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
			SlotsPerSlice: slots, Front: true, UsePrefs: cfg.AllowTimePref,
			EdgeFront: cfg.EdgeFront, EdgeBack: cfg.EdgeBack,
		})
		// 日切构建：启动补建 + 业务日 00:10 定时。参数与 HTTP 建计划同源。
		daily, err := appschedule.NewDailyCron(&appschedule.DailyBuilder{
			Service: scheduleSvc, Tasks: scheduleRepo, Clock: clk, Location: loc,
			Params: appschedule.BuildParams{
				Workers: cfg.Workers, Avg: cfg.AvgAttempt, Rate: cfg.EgressRate,
				Util: corecapacity.UtilDefault, SlotsPerSlice: slots, Front: true,
				UsePrefs: cfg.AllowTimePref, EdgeFront: cfg.EdgeFront, EdgeBack: cfg.EdgeBack,
			},
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
	}

	engine, err := router.New(router.Options{
		Sessions:        issuer,
		Auth:            authSvc,
		Clock:           clk,
		Logger:          logger,
		InsecureCookies: cfg.InsecureCookies,
		Schools:         schools,
		Admins:          admins,
		Accounts:        accounts,
		Users:           users,
		Signing:         signing,
		Schedule:        planning,
		Photos:          photoLib,
		Locations:       locLib,
		Events:          eventLib,
		RateLimit:       guard,
		LoginOrigin:     publicOrigin(cfg.PublicBaseURL),
		TrustedProxies:  trustedProxies,
	})
	if err != nil {
		closeFn()
		return nil, err
	}
	// Close 幂等：srv.Shutdown 触发一次，Run 的收尾再显式调一次。
	var closeOnce sync.Once
	closeAll := func() { closeOnce.Do(closeFn) }
	return &Stack{Engine: engine, DB: db, Worker: workerSvc, Close: closeAll}, nil
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

// egressBurst 是每出口令牌桶的突发深度（账号尝试数）。
// 它只吸收短时抖动，不改长期速率：长期速率由 YIBAN_EGRESS_RATE 决定。
const egressBurst = 6

// workerDeps 是执行循环除限速器之外的依赖。
// 单列出来是为了让 buildWorkerDeps 可以脱离装配被测试：
// 限速器的速率必须与计划容量公式同源，判据要咬住接线那一行。
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
	logger    *slog.Logger
}

// buildWorkerDeps 装配执行循环的依赖，限速器由 buildEgressGate 派生。
//
// 速率必须与计划容量公式取**同一字段**：两处不同源时，缺口门会用
// 调大的 YIBAN_EGRESS_RATE 放行一份执行面跑不完的计划（OCR C2 P1）。
func buildWorkerDeps(cfg *config.Config, d workerDeps) appworker.Deps {
	return appworker.Deps{
		Store:     d.store,
		Creds:     d.creds,
		Signer:    d.signer,
		Egress:    d.egress,
		Limiter:   buildEgressGate(cfg),
		Clock:     d.clock,
		Location:  d.location,
		Locations: d.locations,
		Photos:    d.photos,
		Notifier:  d.notifier,
		Logger:    d.logger,
	}
}

// buildEgressGate 构造执行面的每出口限速器。
func buildEgressGate(cfg *config.Config) *corerate.Limiter {
	return corerate.NewLimiter(cfg.EgressRate, egressBurst)
}

// DeriveAuditKey 从服务器 pepper 派生审计链密钥。
//
// P1 没有独立的 YIBAN_AUDIT_KEY；派生值稳定，重启后仍可校验旧链。
// 轮换 pepper 会让旧链无法再校验，需要一次迁移口径。
// 域标签把本用途与 CSRF、IP 摘要隔开：三者消息不同。
// 凭据加密钥**不**在此派生：它由 YIBAN_CREDENTIAL_KEY 独立注入。
// doctor 的审计链体检复用本函数，保证两侧派生口径只有一份。
func DeriveAuditKey(pepper []byte) ([]byte, error) {
	if len(pepper) == 0 {
		return nil, errors.New("assembly: YIBAN_HASH_PEPPER is required to derive the audit key")
	}
	mac := hmac.New(sha256.New, pepper)
	mac.Write([]byte("yiban.audit.key.v1"))
	return mac.Sum(nil), nil
}

// workerCredentials 把账号用例的明文凭据适配成 signing 契约的形状。
//
// 适配只在此一处：app/account 不依赖 signing 契约，worker 也不直接读库。
// 作用域用 master：执行循环是系统动作，不属于任何管理员的可见面。
type workerCredentials struct {
	svc *appaccount.Service
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

// scheduleDiscoverer 用同校一个活跃账号读取平台窗口。
//
// 窗口属于任务，平台调用却需要登录凭据：借首个活跃账号的登录态。
// 该校没有可用账号时返回 not_found，计划层按缓存或回退窗口继续，
// 不把"没有可借用账号"当成平台故障。
type scheduleDiscoverer struct {
	provider *yiban.Provider
	accounts *appaccount.Service
	tasks    *pg.ScheduleRepo
}

// DiscoverWindow 读一个任务当日的平台窗口。
func (d scheduleDiscoverer) DiscoverWindow(ctx context.Context, schoolTaskID string) (contract.Window, error) {
	task, err := d.tasks.Task(ctx, schoolTaskID)
	if err != nil {
		return contract.Window{}, err
	}
	page, err := d.accounts.List(ctx, corescope.Master(), appaccount.ListFilter{
		SchoolID: task.SchoolID, Status: coreaccount.StatusActive,
	}, "", 1)
	if err != nil {
		return contract.Window{}, err
	}
	if len(page.Items) == 0 {
		return contract.Window{}, coreerrs.New(coreerrs.CodeNotFound,
			"no active account for window discovery")
	}
	acct := page.Items[0]
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

// WorkerNotifier 把执行循环的任务事件转成总线事件（P6）。
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

// ScheduleNotifier 把计划事件转成总线事件（P6）。
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
