//go:build unix

// doctor 的 Unix 平台件：磁盘水位、文件属主、执行位。
// Windows 侧的对应件在 doctor_platform_windows.go。
package main

import (
	"fmt"
	"os"
	"os/user"
	"syscall"
)

// statfsFree 返回目录所在卷的剩余字节。
func statfsFree(dir string) (uint64, error) {
	var st syscall.Statfs_t
	if err := syscall.Statfs(dir, &st); err != nil {
		return 0, fmt.Errorf("statfs %s: %w", dir, err)
	}
	return st.Bavail * uint64(st.Bsize), nil
}

// fileOwnerName 返回文件属主的用户名。ok=false 表示拿不到（调用方跳过）。
func fileOwnerName(dir string) (string, bool) {
	st, err := os.Stat(dir)
	if err != nil {
		return "", false
	}
	sys, ok := st.Sys().(*syscall.Stat_t)
	if !ok {
		return "", false
	}
	u, err := user.LookupId(fmt.Sprint(sys.Uid))
	if err != nil {
		return fmt.Sprint(sys.Uid), true // 有 uid 无名字：仍可与当前 uid 比较
	}
	return u.Username, true
}

// isExecutable 判 POSIX 执行位（属主/组/其他任一即可）。
func isExecutable(path string) (bool, error) {
	st, err := os.Stat(path)
	if err != nil {
		return false, err
	}
	return st.Mode().Perm()&0o111 != 0, nil
}
