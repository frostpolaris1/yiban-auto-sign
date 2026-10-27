//go:build windows

// doctor 的 Windows 平台件：全部显性降级为「拿不到」，
// 对应检查按 SKIP 带原因处理，绝不装绿。
package main

import (
	"errors"
	"os"
)

// errDiskUnsupported 说明 Windows 无 statfs 探针。
var errDiskUnsupported = errors.New("disk free probe unsupported on windows")

// statfsFree 在 Windows 上不可用：调用方按 SKIP 处理。
func statfsFree(dir string) (uint64, error) { return 0, errDiskUnsupported }

// dirStatOf 在 Windows 上拿不到属主、组与 POSIX 权限位：
// 调用方按 SKIP 带原因处理，绝不装绿。
func dirStatOf(dir string) (dirInfo, bool) { return dirInfo{}, false }

// userGroupNames 在 Windows 上不可用：组模型与 POSIX 不同。
func userGroupNames(string) ([]string, bool) { return nil, false }

// isExecutable 恒返回可执行：Windows 的模式位没有 POSIX 语义
// （Go 的 os.Stat 从不报告执行位），本平台不存在「无执行位」状态。
// 生产运行件是 Linux，本实现只服务开发机，不作为安全判据。
func isExecutable(path string) (bool, error) {
	if _, err := os.Stat(path); err != nil {
		return false, err
	}
	return true, nil
}
