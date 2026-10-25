// Command server 运行 HTTP API。
//
// # 功能
// 装配配置、会话签发器与路由，然后服务到 SIGINT 或 SIGTERM。
// 本命令**不**执行数据库迁移、**不**跑后台任务、**不**读命令行参数。
//
// # 归属
// entry 层可执行件。main 只做信号与退出码；装配在 run 与 newServer。
//
// # 复用
// main、run、newServer。
//
// # 口径
// 优雅关闭上限 10s。
// 会话存储在进程内存，故单进程是设计不变量。
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/session"
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

// newServer 装配进程但不启动它。
// 测试通过返回的 server 绑定临时端口。
func newServer(cfg *config.Config, logger *slog.Logger) (*http.Server, error) {
	if cfg == nil {
		return nil, errors.New("server: nil config")
	}
	if logger == nil {
		logger = slog.Default()
	}
	clk := clock.System{}
	sessions := session.NewMemory(clk, nil)
	engine, err := router.New(router.Options{
		Sessions:        sessions,
		Clock:           clk,
		Logger:          logger,
		InsecureCookies: cfg.InsecureCookies,
	})
	if err != nil {
		return nil, err
	}
	return &http.Server{
		Addr:              cfg.HTTPAddr,
		Handler:           engine,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}, nil
}
