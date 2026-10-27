// 外部命令与终端交互的生产实现。
package main

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// execCommand 构造真实进程。
func execCommand(ctx context.Context, name string, args ...string) *exec.Cmd {
	return exec.CommandContext(ctx, name, args...)
}

// execLookPath 找可执行文件。
func execLookPath(tool string) (string, error) {
	return exec.LookPath(tool)
}

// stdinIsTTY 报告 stdin 是否交互终端。
// 非 TTY 时 restore 拒绝无 --force 的执行。
func stdinIsTTY() bool {
	st, err := os.Stdin.Stat()
	if err != nil {
		return false
	}
	return st.Mode()&os.ModeCharDevice != 0
}

// askConfirm 在交互终端上征询「yes」确认。
// 只接受精确的 yes：其他任何输入都视为拒绝。
func askConfirm() bool {
	fmt.Fprint(os.Stderr, "restore: 覆盖目标库不可逆，输入 yes 确认：")
	line, err := bufio.NewReader(os.Stdin).ReadString('\n')
	if err != nil && line == "" {
		return false
	}
	return strings.TrimSpace(line) == "yes"
}
