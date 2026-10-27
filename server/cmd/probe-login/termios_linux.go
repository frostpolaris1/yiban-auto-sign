//go:build linux

package main

import (
	"fmt"
	"syscall"

	"golang.org/x/sys/unix"
)

// readPassword 关回显读一行密码；非终端（重定向/管道）时如实降级为普通读取。
func readPassword(prompt string) (string, error) {
	fd := int(syscall.Stdin)
	old, err := unix.IoctlGetTermios(fd, unix.TCGETS)
	if err != nil {
		return readLine(prompt), nil
	}
	noEcho := *old
	noEcho.Lflag &^= unix.ECHO
	if err := unix.IoctlSetTermios(fd, unix.TCSETS, &noEcho); err != nil {
		return "", err
	}
	defer unix.IoctlSetTermios(fd, unix.TCSETS, old)
	fmt.Print(prompt)
	line := readLine("")
	fmt.Println()
	return line, nil
}
