// Command server 运行 HTTP API。
//
// # 功能
// 装配配置、数据库、会话签发器与路由，然后服务到 SIGINT 或 SIGTERM。
// 配置了 YIBAN_DATABASE_URL 时启动前执行迁移并接线真实认证。
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
	appauth "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/auth"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/router"
)

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
// 有 DSN 时：迁移 → 连接池 → 仓储 → 认证用例。
// 无 DSN 时：内存会话，认证端点回 501，并打印警告。
// **不静默降级**：没有数据库就没有可用登录，警告写在启动日志里。
func buildStack(cfg *config.Config, logger *slog.Logger) (*appStack, error) {
	if cfg == nil {
		return nil, errors.New("server: nil config")
	}
	if logger == nil {
		logger = slog.Default()
	}
	clk := clock.System{}
	var (
		issuer  auth.SessionIssuer
		authSvc *appauth.Service
		db      *pg.DB
		closeFn = func() {}
	)

	if strings.TrimSpace(cfg.DatabaseURL) == "" {
		issuer = session.NewMemory(clk, cfg.HashPepper)
		logger.Warn("auth endpoints are disabled: YIBAN_DATABASE_URL is empty")
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
	}

	engine, err := router.New(router.Options{
		Sessions:        issuer,
		Auth:            authSvc,
		Clock:           clk,
		Logger:          logger,
		InsecureCookies: cfg.InsecureCookies,
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
// 域标签把本用途与 CSRF、IP 摘要隔开：三者消息不同。
func deriveAuditKey(pepper []byte) ([]byte, error) {
	if len(pepper) == 0 {
		return nil, errors.New("server: YIBAN_HASH_PEPPER is required to derive the audit key")
	}
	mac := hmac.New(sha256.New, pepper)
	mac.Write([]byte("yiban.audit.key.v1"))
	return mac.Sum(nil), nil
}
