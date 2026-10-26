// Command server 运行 HTTP API。
//
// # 功能
// 收到 SIGINT/SIGTERM 后交给 internal/assembly 的 Run 收尾。
// 装配、迁移、执行循环与优雅关闭全部在 assembly：
// cmd/server 与 cmd/cli 的 server 子命令共享同一份实现。
// 本命令**不**读命令行参数、**不**复制装配代码。
//
// # 归属
// entry 层可执行件。main 只做信号与退出码。
//
// # 复用
// assembly.Run。
//
// # 口径
// 优雅关闭上限 10s（assembly.shutdownBudget）。
// 退出码：Run 返回错误即 1。
package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/assembly"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := assembly.Run(ctx, logger); err != nil {
		logger.Error("server stopped", "error", err.Error())
		os.Exit(1)
	}
}
