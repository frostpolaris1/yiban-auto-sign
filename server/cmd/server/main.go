// Command server 运行 HTTP API。
//
// # 功能
// 装配配置、数据库、会话签发器、四个业务域与路由，然后服务到 SIGINT 或 SIGTERM。
// 配置了 YIBAN_DATABASE_URL 时启动前执行迁移，
// 并接线真实认证、用户、学校、管理员与账号端点。
// 本命令**不**跑后台任务、**不**读命令行参数。
//
// # 归属
// entry 层可执行件。main 只做信号与退出码；装配在 run 与 buildStack。
//
// # 复用
// main、run、newServer、buildStack、appStack。
//
// # 口径
// 优雅关闭上限 10s。
// 无数据库时认证端点不可用，进程打印警告，健康检查仍可服务。
package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/session"
	appaccount "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/account"
	appadmin "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/admin"
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	appegress "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/egress"
	appschool "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/school"
	appuser "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/user"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corerate "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/rate"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	accounthttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/account"
	adminhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/admin"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
	schoolhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/school"
	signinghttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/signing"
	userhttp "github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/user"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/providers/yiban"
)

// accountCapacityLimit 是 P1 的活跃账号上限（裁决 12.11）。
// P1 不提供配置项；改上限只改本常量。
const accountCapacityLimit = 10000

// userCapacityLimit 是 P1 的活跃用户上限（裁决 12.11）。
// 与账号上限同值，两道约束各自在插入事务内判定。
const userCapacityLimit = 10000

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := run(ctx, logger); err != nil {
		logger.Error("server stopped", "error", err.Error())
		os.Exit(1)
	}
}

func run(ctx context.Context, logger *slog.Logger) error {
	cfg, err := config.Load()
	if err != nil {
		return err
	}
	srv, err := newServer(cfg, logger)
	if err != nil {
		return err
	}

	errc := make(chan error, 1)
	go func() { errc <- srv.ListenAndServe() }()

	select {
	case <-ctx.Done():
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return srv.Shutdown(shutdown)
	case err := <-errc:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return err
	}
}

// appStack 是装配结果。engine 已就绪；db 在无数据库时为 nil。
type appStack struct {
	engine http.Handler
	db     *pg.DB
	close  func()
}

// newServer 装配进程但不启动它。
// 测试通过返回的 server 绑定临时端口。
func newServer(cfg *config.Config, logger *slog.Logger) (*http.Server, error) {
	if logger == nil {
		logger = slog.Default()
	}
	stack, err := buildStack(cfg, logger)
	if err != nil {
		return nil, err
	}
	srv := &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           stack.engine,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	// 关闭路径释放连接池。srv.Close 不触发，但它只用于测试。
	srv.RegisterOnShutdown(stack.close)
	return srv, nil
}

// buildStack 装配数据库、用例与路由。
//
// 有 DSN 时：迁移 → 连接池 → 仓储 → 认证与四个业务域用例。
// 无 DSN 时：内存会话；认证与四个业务域端点回 501，并打印警告。
// **不静默降级**：没有数据库就没有可用登录，警告写在启动日志里。
// buildStack 装配全部服务与真实签到提供方。
func buildStack(cfg *config.Config, logger *slog.Logger) (*appStack, error) {
	if cfg == nil {
		return nil, errors.New("server: nil config")
	}
	if logger == nil {
		logger = slog.Default()
	}
	clk := clock.System{}
	var (
		issuer   auth.SessionIssuer
		authSvc  *appauth.Service
		schools  *schoolhttp.Handler
		admins   *adminhttp.Handler
		accounts *accounthttp.Handler
		users    *userhttp.Handler
		signing  *signinghttp.Handler
		db       *pg.DB
		closeFn  = func() {}
	)

	if strings.TrimSpace(cfg.DatabaseURL) == "" {
		issuer = session.NewMemory(clk, cfg.HashPepper)
		logger.Warn("auth endpoints are disabled: YIBAN_DATABASE_URL is empty")
		logger.Warn("user, school, admin and account endpoints return 501: YIBAN_DATABASE_URL is empty")
	} else {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()

		if _, err := pg.Migrate(ctx, cfg.DatabaseURL); err != nil {
			return nil, fmt.Errorf("server: migrate: %w", err)
		}
		var err error
		db, err = pg.Open(ctx, pg.Options{DSN: cfg.DatabaseURL})
		if err != nil {
			return nil, fmt.Errorf("server: open database: %w", err)
		}
		closeFn = db.Close

		auditKey, err := deriveAuditKey(cfg.HashPepper)
		if err != nil {
			closeFn()
			return nil, err
		}
		audits, err := pg.NewAuditRepo(db, auditKey)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("server: audit repo: %w", err)
		}
		issuer, err = pg.NewSessionIssuer(pg.NewSessionRepo(db), audits, cfg.HashPepper, clk)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("server: session issuer: %w", err)
		}
		authSvc, err = appauth.NewService(appauth.Deps{
			Users:    pg.NewUserRepo(db),
			Sessions: issuer,
			Resets:   pg.NewPasswordResetRepo(db),
			Audits:   audits,
			Tx:       db,
			Clock:    clk,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("server: auth service: %w", err)
		}

		// 凭据钥独立于 pepper：轮换 pepper 只让摘要与审计链失效，
		// 不能连带让凭据永久不可解密（裁决：W2C-R2-04）。
		cipher, err := appaccount.NewCipher(cfg.CredentialKey)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("server: credential cipher: %w", err)
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
			return nil, fmt.Errorf("server: school service: %w", err)
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
			return nil, fmt.Errorf("server: admin service: %w", err)
		}
		accountSvc, err := appaccount.NewService(appaccount.Deps{
			Accounts: pg.NewAccountRepo(db),
			Schools:  schoolRepo,
			Audit:    audits,
			Tx:       db,
			Clock:    clk,
			Cipher:   cipher,
			Limit:    accountCapacityLimit,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("server: account service: %w", err)
		}
		// 用户用例复用同一数据库下的用户仓储实例（仓储无状态）：建号走 Create，
		// 它的容量守卫与账号上限同形（裁决 12.11）。
		userRepo := pg.NewUserRepo(db)
		userSvc, err := appuser.NewService(appuser.Deps{
			Users: userRepo,
			Audit: audits,
			Tx:    db,
			Clock: clk,
			Limit: userCapacityLimit,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("server: user service: %w", err)
		}
		schools = schoolhttp.NewHandler(schoolSvc, adminSvc)
		admins = adminhttp.NewHandler(adminSvc, adminSvc)
		accounts = accounthttp.NewHandler(accountSvc, adminSvc)
		users = userhttp.NewHandler(userSvc, adminSvc)
		// 一个 provider 同时服务"凭据校验"与"执行循环"：两者必须走同一
		// 六请求链实现，避免校验用一套、签到用另一套的漂移。
		provider := yiban.NewProvider(yiban.New(yiban.Config{Clock: clk}))
		signing = signinghttp.NewHandler(accountSvc, adminSvc, provider)

		// 执行循环：手动签到入队 + 进程启动时回收孤儿 running 行。
		// 限速与出口参数暂用模块缺省；配置面（名册键）随 P4-F 收口统一接入。
		egressSvc, err := appegress.NewService(appegress.Deps{
			Store: pg.NewEgressRepo(db), Clock: clk,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("server: egress service: %w", err)
		}
		loc, err := clock.LoadLocation(cfg.Timezone)
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("server: load timezone %q: %w", cfg.Timezone, err)
		}
		workerSvc, err := appworker.NewService(appworker.Deps{
			Store:    pg.NewScheduleRepo(db),
			Creds:    workerCredentials{svc: accountSvc},
			Signer:   provider,
			Egress:   egressSvc,
			Limiter:  corerate.NewLimiter(corerate.DefaultRate, 6),
			Clock:    clk,
			Location: loc,
		})
		if err != nil {
			closeFn()
			return nil, fmt.Errorf("server: worker service: %w", err)
		}
		if n, recErr := workerSvc.Recover(ctx); recErr != nil {
			closeFn()
			return nil, fmt.Errorf("server: recover orphan tasks: %w", recErr)
		} else if n > 0 {
			logger.Info("worker: recovered orphan running rows", "count", n)
		}
		signing.WithManual(workerSvc)
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
	})
	if err != nil {
		closeFn()
		return nil, err
	}
	return &appStack{engine: engine, db: db, close: closeFn}, nil
}

// deriveAuditKey 从服务器 pepper 派生审计链密钥。
//
// P1 没有独立的 YIBAN_AUDIT_KEY；派生值稳定，重启后仍可校验旧链。
// 轮换 pepper 会让旧链无法再校验，需要一次迁移口径。
// 域标签把本用途与 CSRF、IP 摘要隔开：三者消息不同。
// 凭据加密钥**不**在此派生：它由 YIBAN_CREDENTIAL_KEY 独立注入。
func deriveAuditKey(pepper []byte) ([]byte, error) {
	if len(pepper) == 0 {
		return nil, errors.New("server: YIBAN_HASH_PEPPER is required to derive the audit key")
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
