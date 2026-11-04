// Command server 运行 HTTP API。
//
// # 功能
// 收到 SIGINT/SIGTERM 后交给 internal/assembly 的 Run 收尾。
// `--version` 打印版本身份后退出；其他参数是用法错误。
// 装配、迁移、执行循环与优雅关闭全部在 assembly：
// cmd/server 与 cmd/cli 的 server 子命令共享同一份实现。
// 本命令**不**读其他命令行参数、**不**复制装配代码。
//
// # 归属
// entry 层可执行件。main 只做版本、信号与退出码。
//
// # 复用
// buildinfo.Current；assembly.Run。
//
// # 口径
// 优雅关闭上限 10s（assembly.shutdownBudget）。
// 退出码：Run 返回错误即 1；用法错误 2。
// 未知参数一律报错：静默忽略会让 systemd 单元里的笔误进入生产。
package main

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/assembly"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/logfield"
)

func main() {
	args := os.Args[1:]
	switch {
	case len(args) == 0:
	case len(args) == 1 && isVersionArg(args[0]):
		_, _ = io.WriteString(os.Stdout, buildinfo.Current().String())
		return
	default:
		_, _ = fmt.Fprintf(os.Stderr, "yiban-server: 不认识的参数 %q\n", strings.Join(args, " "))
		os.Exit(2)
	}

	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	if err := assembly.Run(ctx, logger); err != nil {
		logger.Error("server stopped", logfield.KeyError, err.Error())
		os.Exit(1)
	}
}

// isVersionArg 报告参数是否是版本查询。接受 Unix 常见的单/双横线两种写法。
func isVersionArg(arg string) bool {
	return arg == "--version" || arg == "-version"
}
