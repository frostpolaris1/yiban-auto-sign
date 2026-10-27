//go:build !linux

package main

import "fmt"

// readPassword 在非 Linux 平台没有 termios 通道，降级为回显读取并明说。
// 生产探针只在 Linux 上跑；此文件只为让本包在任意平台可编译。
func readPassword(prompt string) (string, error) {
	fmt.Println("(警告: 当前平台不支持关闭回显，密码将显示在屏幕上)")
	return readLine(prompt), nil
}
