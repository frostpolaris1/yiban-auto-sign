// version 子命令：打印版本身份。
//
// # 功能
// 输出 version、commit、build_time 三行；值来自构建期 -ldflags 注入。
// 未注入时显性 dev/unknown，不编造。
//
// # 口径
// 退出码：成功 0；多余参数是用法错误 2。
package main

import (
	"fmt"
	"io"
	"strings"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/buildinfo"
)

// runVersion 打印版本身份。返回进程退出码。
func runVersion(args []string, stdout, stderr io.Writer) int {
	if len(args) > 0 {
		_, _ = fmt.Fprintf(stderr, "yiban-cli version: 不认识的参数 %q（本子命令无选项）\n",
			strings.Join(args, " "))
		return 2
	}
	_, _ = io.WriteString(stdout, buildinfo.Current().String())
	return 0
}
