// Command yiban-cli 是 Go 服务端的运维命令行。
//
// # 功能
// 四个子命令：server（起服务，与 cmd/server 同实现）、
// migrate（应用迁移并打印版本）、doctor（故障体检）、
// healthcheck（对 /healthz 做一次探活，容器 healthcheck 用）。
// backup 与 restore 在片 3 加入。
// 本命令**不**进 OpenAPI（派单 P7：CLI 不在 openapi 契约面）。
//
// # 归属
// entry 层可执行件。装配与关闭在 internal/assembly；
// 体检在 doctor.go；备份恢复在 backup.go。
//
// # 口径
// 退出码：成功 0；用法错误 2；体检有 FAIL 或运行失败 1。
// 子命令不交互：需要确认的动作必须显式给 flag（agent/CI 无 stdin）。
package main

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"
)

func main() {
	if len(os.Args) < 2 {
		usage(os.Stderr)
		os.Exit(2)
	}
	var code int
	switch os.Args[1] {
	case "server":
		code = runServer(os.Args[2:])
	case "migrate":
		code = runMigrate(os.Args[2:], os.Stdout, os.Stderr)
	case "doctor":
		code = runDoctor(os.Args[2:], os.Stdout, os.Stderr)
	case "healthcheck":
		code = runHealthcheck(os.Args[2:], os.Stderr)
	case "--help", "-h", "help":
		usage(os.Stdout)
	default:
		fmt.Fprintf(os.Stderr, "yiban-cli: 未知子命令 %q\n", os.Args[1])
		usage(os.Stderr)
		code = 2
	}
	os.Exit(code)
}

// usage 打印子命令清单。新增子命令在此登记。
func usage(w *os.File) {
	fmt.Fprint(w, `用法: yiban-cli <子命令> [选项]

子命令:
  server       启动 HTTP API（与 cmd/server 同一装配与优雅关闭）
  migrate      应用数据库迁移并打印 schema 版本
  doctor       故障模式体检（每条 FAIL 附修复命令）
  healthcheck  探活 /healthz（容器 healthcheck 用）
  help         打印本清单
`)
}

// signalCtx 监听 SIGINT/SIGTERM，返回可取消的 ctx。
func signalCtx() (context.Context, func()) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}

// cliLogger 是子命令共用的 JSON 日志器。
func cliLogger() *slog.Logger {
	return slog.New(slog.NewJSONHandler(os.Stdout, nil))
}
