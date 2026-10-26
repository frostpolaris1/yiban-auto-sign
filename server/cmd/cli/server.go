// server 子命令：转调 assembly.Run，与 cmd/server 共享同一份装配与关闭。
package main

import (
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/assembly"
)

// runServer 启动服务直到收到 SIGINT/SIGTERM。
// 返回进程退出码：装配或关闭失败即 1。
func runServer(_ []string) int {
	logger := cliLogger()
	ctx, stop := signalCtx()
	defer stop()
	if err := assembly.Run(ctx, logger); err != nil {
		logger.Error("server stopped", "error", err.Error())
		return 1
	}
	return 0
}
