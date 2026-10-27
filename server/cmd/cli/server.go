// server 子命令：转调 assembly.Run，与 cmd/server 共享同一份装配与关闭。
package main

import (
	"fmt"
	"os"
	"strings"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/assembly"
)

// runAssembly 是转调接缝：测试注入替身，避免测试启动真实服务。
var runAssembly = assembly.Run

// runServer 启动服务直到收到 SIGINT/SIGTERM。
// 返回进程退出码：装配或关闭失败即 1；多余参数是用法错误 2。
// 多余参数一律报错：静默忽略会让 `server --post 8080` 这类笔误进入生产。
func runServer(args []string) int {
	if len(args) > 0 {
		fmt.Fprintf(os.Stderr, "yiban-cli server: 不认识的参数 %q（本子命令无选项）\n", strings.Join(args, " "))
		return 2
	}
	logger := cliLogger()
	ctx, stop := signalCtx()
	defer stop()
	if err := runAssembly(ctx, logger); err != nil {
		logger.Error("server stopped", "error", err.Error())
		return 1
	}
	return 0
}
