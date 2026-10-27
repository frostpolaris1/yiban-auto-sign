// healthcheck 子命令：对 /healthz 做一次探活。
//
// # 功能
// GET http://addr/healthz，期望 200。它是容器 healthcheck 的探针：
// distroless 镜像里没有 shell 与 curl，由本子命令承担探活。
//
// # 口径
// 退出码：200 即 0；连接失败或非 200 即 1。
package main

import (
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
)

// runHealthcheck 探活一次。返回进程退出码。
func runHealthcheck(args []string, stderr io.Writer) int {
	fs := newFlagSet("healthcheck", stderr)
	addr := fs.String("addr", config.DefaultHTTPAddr, "被探的 host:port")
	timeout := fs.Duration("timeout", 3*time.Second, "单次请求超时")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	client := &http.Client{Timeout: *timeout}
	resp, err := client.Get("http://" + *addr + "/healthz")
	if err != nil {
		fmt.Fprintf(stderr, "healthcheck: %v\n", err)
		return 1
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	if resp.StatusCode != http.StatusOK {
		fmt.Fprintf(stderr, "healthcheck: status = %d, want 200\n", resp.StatusCode)
		return 1
	}
	return 0
}
